# -*- coding: utf-8 -*-
"""微信 4.x：环境检测 / 密钥提取 / 数据库解密 / 提权一体化模块。

供主程序（微信聊天记录查看器）调用：
- detect_environment()      检测微信进程、版本、数据账号
- elevate_and_decrypt(root) 提权并解密整个 xwechat_files 下可解的账号
- run_headless_decrypt()    exe 以 --decrypt 参数启动时的解密入口
"""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from Crypto.Cipher import AES

SQLITE_HEADER = b"SQLite format 3" + bytes(1)
KEY_SZ = 32
PAGE_SZ = 4096
SALT_SZ = 16
IV_SZ = 16
HMAC_SZ = 64
RESERVE_SZ = (IV_SZ + HMAC_SZ + 15) // 16 * 16

# 表情文件加密：AES-128-CBC + PKCS7，key = IV = md5(f"{seed}{wxid}EMOTICON") 前 16 字节
EMO_MAGICS = (b"\x89PNG", b"GIF8", b"\xff\xd8\xff", b"wxgf", b"RIFF")
_RE_SEED = re.compile(rb"(?<![0-9])(\d{8,12})(?![0-9])")


def _weixin_main_pid() -> int | None:
    """微信主进程：cmdline 最短的那个（其余是渲染子进程）。"""
    import psutil
    procs = [
        (p.pid, p.info["cmdline"] or [])
        for p in psutil.process_iter(["pid", "name", "cmdline"])
        if p.info["name"] and p.info["name"].lower() == "weixin.exe"
    ]
    if not procs:
        return None
    return min(procs, key=lambda x: len(x[1]))[0]


def _scan_emoticon_seeds(pid: int) -> set[int]:
    """扫描 Weixin.exe 主进程内存中的 seed 候选（9~10 位十进制数）。

    注意：本函数在后台线程运行，但正则匹配大块数据会长时间占用 GIL，
    导致主线程（Tk 事件循环）被饿死、界面无响应。因此：
    - 块大小用 4MB（缩短单次持锁时间）
    - 每块之间 time.sleep(0) 主动让出 GIL
    - 跳过超大区域（>64MB 通常不含目标数字，避免长时间扫描）
    """
    class MBI(ctypes.Structure):
        _fields_ = [
            ("BaseAddress", ctypes.c_uint64),
            ("AllocationBase", ctypes.c_uint64),
            ("AllocationProtect", ctypes.c_uint32),
            ("_pad1", ctypes.c_uint32),
            ("RegionSize", ctypes.c_uint64),
            ("State", ctypes.c_uint32),
            ("Protect", ctypes.c_uint32),
            ("Type", ctypes.c_uint32),
            ("_pad2", ctypes.c_uint32),
        ]

    kernel32 = ctypes.windll.kernel32
    READABLE = {2, 4, 8, 16, 32, 64, 128}
    MEM_COMMIT = 0x1000
    MEM_MAPPED = 0x40000   # 映射文件区域，通常不含种子数字
    MAX_REGION = 64 * 1024 * 1024   # 跳过超大区域，避免长时间持锁
    CHUNK = 1 * 1024 * 1024         # 1MB/块，进一步缩短单次正则持锁时间

    def read_mem(h, addr, sz):
        buf = ctypes.create_string_buffer(sz)
        n = ctypes.c_size_t(0)
        if kernel32.ReadProcessMemory(h, ctypes.c_uint64(addr), buf, sz,
                                      ctypes.byref(n)):
            return buf.raw[: n.value]
        return None

    def enum_regions(h):
        regs = []
        addr = 0
        mbi = MBI()
        while addr < 0x7FFFFFFFFFFF:
            if kernel32.VirtualQueryEx(h, ctypes.c_uint64(addr),
                                       ctypes.byref(mbi),
                                       ctypes.sizeof(mbi)) == 0:
                break
            if (mbi.State == MEM_COMMIT and mbi.Protect in READABLE
                    and mbi.Type != MEM_MAPPED
                    and 0 < mbi.RegionSize < MAX_REGION):
                regs.append((mbi.BaseAddress, mbi.RegionSize))
            nxt = mbi.BaseAddress + mbi.RegionSize
            if nxt <= addr:
                break
            addr = nxt
        return regs

    h = kernel32.OpenProcess(0x0410, False, pid)
    if not h:
        return set()
    seeds: set[int] = set()
    try:
        for base, size in enum_regions(h):
            off = 0
            tail = b""
            while off < size:
                cur = min(CHUNK, size - off)
                chunk = read_mem(h, base + off, cur)
                if chunk:
                    data = tail + chunk
                    for m in _RE_SEED.finditer(data):
                        v = int(m.group(0))
                        if 100_000_000 < v < 4_000_000_000:
                            seeds.add(v)
                    tail = data[-64:]
                else:
                    tail = b""
                off += cur
                # 主动让出 GIL，避免饿死主线程（界面保持响应）
                time.sleep(0)
    finally:
        kernel32.CloseHandle(h)
    return seeds


def find_emoticon_key(account_dir, log=print, seeds=None):
    """从运行中的微信内存提取表情密钥并验证。

    返回 (seed, wxid, key_bytes) 或 None。key = md5(f"{seed}{wxid}EMOTICON") 前 16 字节，
    用任意表情文件的 C0 块（AES-CBC key=IV 解密后命中图片魔数）验证。
    """
    account_dir = Path(account_dir)
    emo_dir = account_dir / "business" / "emoticon"
    if not emo_dir.is_dir():
        emo_dir = account_dir / "emoticon"
    sample = None
    if emo_dir.is_dir():
        for sub in ("Persist", "Thumb"):
            base = emo_dir / sub
            if not base.is_dir():
                continue
            for p in base.rglob("*"):
                if p.is_file() and len(p.name) >= 32:
                    sample = p
                    break
            if sample:
                break
    if not sample:
        log("未找到表情样本文件")
        return None
    try:
        c0 = sample.read_bytes()[:16]
    except OSError:
        return None

    if seeds is None:
        pid = _weixin_main_pid()
        if not pid:
            log("未找到运行中的 Weixin.exe")
            return None
        log(f"正在扫描 Weixin.exe (PID {pid}) 内存中的表情密钥…")
        seeds = _scan_emoticon_seeds(pid)
    if not seeds:
        log("未扫描到 seed 候选")
        return None
    if seeds is not None:
        log(f"扫描到 {len(seeds)} 个 seed 候选，正在验证…")

    dirname = account_dir.name
    wxid_cands = {dirname}
    if "_" in dirname:
        wxid_cands.add(dirname.rsplit("_", 1)[0])
    wxid_cands.add(dirname.replace("_b487", ""))
    wxid_cands.add(dirname.replace("_68b0", ""))

    def derive(seed, wxid):
        d = hashlib.md5(f"{seed}{wxid}EMOTICON".encode()).hexdigest()
        return bytes.fromhex(d)[:16]

    def verify(key, block):
        try:
            dec = AES.new(key, AES.MODE_CBC, key).decrypt(block)
            return dec[:4] in EMO_MAGICS
        except Exception:
            return False

    for seed in sorted(seeds):
        for wxid in sorted(wxid_cands):
            key = derive(seed, wxid)
            if verify(key, c0):
                log(f"表情密钥命中：seed={seed} wxid={wxid} key={key.hex()}")
                return seed, wxid, key
    log("未找到匹配的表情密钥")
    return None


# 图片 V2 解密参数（AES-128-ECB + XOR）：
#   aes_key = md5(f"{uin}{wxid}")[:16]（16 字符 ASCII）
#   xor_key = uin & 0xFF
V2_MAGIC = b"\x07\x08\x56\x32\x08\x07"
V1_MAGIC = b"\x07\x08\x56\x31\x08\x07"
V1_IMAGE_KEY = b"cfcd208495d565ef"
IMG_MAGICS = (b"\xff\xd8\xff", b"\x89PNG", b"GIF", b"RIFF", b"wxgf")


def _normalize_wxid(raw: str) -> str:
    """wxid_xxx_68b0 -> wxid_xxx（去掉 _<4hex> 后缀，那是 md5(uin)[:4]）。"""
    aid = (raw or "").strip()
    if not aid:
        return ""
    if aid.lower().startswith("wxid_"):
        m = re.match(r"^(wxid_[^_]+)", aid, re.IGNORECASE)
        return m.group(1) if m else aid
    m = re.match(r"^(.+)_([a-zA-Z0-9]{4})$", aid)
    return m.group(1) if m else aid


def _image_templates(account_dir, max_templates=3) -> list[bytes]:
    """收集 V2 .dat 文件的模板密文（[0xF:0x1F] 16B）用于密钥验证。"""
    attach = Path(account_dir) / "msg" / "attach"
    if not attach.is_dir():
        return []
    out, seen = [], set()
    for p in attach.rglob("*"):
        if not p.is_file() or not p.name.endswith(".dat"):
            continue
        try:
            with open(p, "rb") as f:
                data = f.read(0x20)
        except OSError:
            continue
        if len(data) >= 0x1F and data[:6] == V2_MAGIC:
            ct = data[0xF:0x1F]
            if ct not in seen:
                seen.add(ct)
                out.append(ct)
                if len(out) >= max_templates:
                    break
    return out


def _verify_image_key(aes_ascii: bytes, templates) -> bool:
    if not templates or len(aes_ascii) < 16:
        return False
    try:
        return all(
            AES.new(aes_ascii[:16], AES.MODE_ECB).decrypt(ct).startswith(IMG_MAGICS)
            for ct in templates)
    except Exception:
        return False


def _derive_image_key(uin: int, wxid: str):
    aes = hashlib.md5(f"{uin}{wxid}".encode()).hexdigest()[:16].encode()
    return uin & 0xFF, aes


def _img_key_cache_path(account_dir) -> Path:
    return Path(account_dir) / ".wx_viewer_imgkey.json"


def load_image_key_cache(account_dir):
    """读取上次成功找到的图片密钥缓存，并用当前 .dat 模板验证。

    命中返回 (xor_key, aes_key_bytes)；缓存缺失/失效返回 None。
    """
    p = _img_key_cache_path(account_dir)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        uin = int(data.get("uin", 0))
        wxid = data.get("wxid", "")
        if not uin or not wxid:
            return None
        xor, aes = _derive_image_key(uin, wxid)
        templates = _image_templates(account_dir)
        if _verify_image_key(aes, templates):
            return xor, aes
    except Exception:
        pass
    return None


def save_image_key_cache(account_dir, uin: int, wxid: str):
    """把成功验证过的图片密钥写入账号目录缓存，下次秒开。"""
    try:
        p = _img_key_cache_path(account_dir)
        p.write_text(
            json.dumps({"uin": int(uin), "wxid": str(wxid)},
                       ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass


def find_image_keys(account_dir, log=print, seeds=None):
    """提取图片 V2 解密密钥 (xor_key, aes_key_bytes)，无需查看图片。

    优先内存扫描 uin（= 表情 seed）；失败则用 wxid 后缀 + .dat 模板暴力枚举。
    """
    account_dir = Path(account_dir)
    dirname = account_dir.name
    wxid_norm = _normalize_wxid(dirname)
    templates = _image_templates(account_dir)
    if not templates:
        log("未找到 V2 图片模板")
        return None

    def try_uin(uin: int) -> bool:
        xor, aes = _derive_image_key(uin, wxid_norm)
        return _verify_image_key(aes, templates)

    # 路径0：上次成功缓存（免扫描，秒开）
    cached = load_image_key_cache(account_dir)
    if cached:
        log("命中图片密钥缓存，跳过扫描")
        return cached

    # 路径1：内存扫描 uin（数字候选，与表情 seed 同源）
    if seeds is not None:
        for uin in seeds:
            if try_uin(uin):
                xor, aes = _derive_image_key(uin, wxid_norm)
                log(f"图片密钥命中：uin={uin} aes={aes.decode()} xor=0x{xor:02x}")
                save_image_key_cache(account_dir, uin, wxid_norm)
                return xor, aes
    else:
        pid = _weixin_main_pid()
        if pid:
            log(f"正在扫描 Weixin.exe (PID {pid}) 内存中的图片密钥…")
            for uin in _scan_emoticon_seeds(pid):
                if try_uin(uin):
                    xor, aes = _derive_image_key(uin, wxid_norm)
                    log(f"图片密钥命中：uin={uin} aes={aes.decode()} xor=0x{xor:02x}")
                    save_image_key_cache(account_dir, uin, wxid_norm)
                    return xor, aes

    # 路径2：暴力枚举（wxid 后缀 = md5(str(uin))[:4]，xor_key = uin & 0xFF）
    m = re.match(r"^(.+)_([0-9a-fA-F]{4})$", dirname)
    if m:
        suffix = m.group(2).lower()
        # 先从 .dat 尾部 XOR 推断 xor_key（JPEG 尾部固定 0xFF 0xD9），
        # 把枚举空间从 2^32 缩小到 2^24（约 1600 万，十几秒内完成）。
        xor_bucket = _infer_xor_bucket(account_dir)
        if xor_bucket is None:
            log("无法从尾部推断 xor_key，跳过暴力枚举")
            log("未找到图片密钥")
            return None
        log(f"内存扫描未命中，按 xor=0x{xor_bucket:02x} + 后缀 {suffix} 枚举…")
        for uin in range(xor_bucket, 2 ** 32, 256):
            if hashlib.md5(str(uin).encode()).hexdigest()[:4] == suffix:
                if try_uin(uin):
                    xor, aes = _derive_image_key(uin, wxid_norm)
                    log(f"图片密钥命中：uin={uin} aes={aes.decode()} xor=0x{xor:02x}")
                    save_image_key_cache(account_dir, uin, wxid_norm)
                    return xor, aes
    log("未找到图片密钥")
    return None


def _infer_xor_bucket(account_dir) -> int | None:
    """从 V2 .dat 尾部 XOR 推断 xor_key（= uin & 0xFF）。

    解密后的 JPEG 以 0xFF 0xD9 结尾，加密尾部为 (0xFF^xor, 0xD9^xor)，
    取任意 JPEG 缩略图 _t.dat 的尾部倒数第 2 字节即可反推 xor。
    """
    attach = Path(account_dir) / "msg" / "attach"
    if not attach.is_dir():
        return None
    for p in attach.rglob("*.dat"):
        if not p.name.endswith("_t.dat"):
            continue
        try:
            with open(p, "rb") as f:
                head = f.read(0x1F)
                f.seek(-8, 2)
                tail = f.read()
        except OSError:
            continue
        if len(head) >= 0x1F and len(tail) >= 4 and head[:6] == V2_MAGIC:
            # 尾部 XOR 数据倒数第 2 字节对应明文 JPEG 的 0xFF
            cand = tail[-2] ^ 0xFF
            if 0 <= cand <= 255:
                return cand
    return None


def default_xwechat_root() -> Path:
    return Path.home() / "Documents" / "xwechat_files"

def _normalize_root_candidate(p: Path) -> Path | None:
    """把候选路径归一化到 xwechat_files 数据根目录（存在即返回）。"""
    if not p:
        return None
    p = p.resolve() if p.exists() else p
    # 微信进程打开的路径形如 ...\\xwechat_files\\<wxid>\\db_storage\\...，向上回溯到 xwechat_files
    for part in p.parts:
        if part == "xwechat_files":
            idx = p.parts.index(part)
            root = Path(*p.parts[:idx + 1])
            return root if root.is_dir() else None
    if p.is_dir() and p.exists():
        return p
    return None

def _root_from_running_weixin() -> Path | None:
    """从运行中微信进程自报的已打开数据库路径，反推它的 xwechat_files 数据根目录。"""
    try:
        import psutil
        for p in psutil.process_iter(["pid", "name"]):
            if (p.info["name"] or "").lower() != "weixin.exe":
                continue
            try:
                for f in p.open_files():
                    path = f.path or ""
                    if "xwechat_files" not in path.lower():
                        continue
                    if not path.lower().endswith(".db"):
                        continue
                    root = _normalize_root_candidate(Path(path))
                    if root:
                        return root
            except Exception:
                continue
    except Exception:
        pass
    return None

def _root_from_env() -> Path | None:
    """从环境变量（可被用户/脚本显式指定）读取数据根目录。"""
    for var in ("XWECHAT_ROOT", "WECHAT_DB_ROOT", "WEIXIN_DATA_DIR"):
        v = (os.environ.get(var) or "").strip()
        if v and Path(v).is_dir():
            return _normalize_root_candidate(Path(v)) or Path(v)
    return None

def _root_scan_drives() -> Path | None:
    """兜底：在常见数据盘上扫描 xwechat_files 目录（只深一层，避免全盘遍历）。"""
    bases = []
    try:
        bases.append(Path.home() / "Documents")
        for drive in ("C", "D", "E", "F"):
            d = Path(f"{drive}:\\")
            if d.is_dir():
                bases.append(d)
                bases.append(d)
    except Exception:
        pass
    for base in bases:
        try:
            for child in base.iterdir():
                if child.is_dir() and child.name.lower() == "xwechat_files":
                    return child
        except Exception:
            continue
    return None

def default_xwechat_root() -> Path:
    """多候选发现微信数据根目录：微信自报 > 环境变量 > 默认用户目录 > 扫描常见盘符。"""
    for finder in (_root_from_running_weixin, _root_from_env, _root_scan_drives):
        root = finder()
        if root and root.is_dir():
            return root
    return Path.home() / "Documents" / "xwechat_files"


def find_weixin_process() -> dict | None:
    """返回运行中的 Weixin.exe 进程信息（pid/exe/cmdline）。"""
    import psutil
    for p in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        if (p.info["name"] or "").lower() == "weixin.exe":
            return p.info
    return None


def wechat_version(proc: dict | None = None) -> str:
    """获取微信版本号：优先取运行进程 exe 的版本信息。"""
    proc = proc or find_weixin_process()
    if proc and proc.get("exe"):
        exe = proc["exe"]
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-Item -LiteralPath '{exe}').VersionInfo.FileVersion"],
                capture_output=True, text=True, timeout=15, creationflags=0x08000000)
            ver = (result.stdout or "").strip()
            if ver:
                return ver
        except Exception:
            pass
        # 兜底：微信 4.x 的安装目录名即版本号（如 4.1.9.35）
        parent = Path(exe).parent
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", parent.name or ""):
            return parent.name
    return ""


def list_accounts(root: Path | None = None) -> list[dict]:
    """扫描 xwechat_files 下的账号目录。"""
    root = root or default_xwechat_root()
    accounts = []
    if not root.is_dir():
        return accounts
    for account in sorted(root.iterdir()):
        if not account.is_dir():
            continue
        db_storage = account / "db_storage"
        if not db_storage.is_dir():
            continue
        db_files = list(db_storage.rglob("*.db"))
        decrypted = list(db_storage.rglob("*.decrypted.db"))
        if not db_files:
            continue
        accounts.append({
            "dir": account,
            "name": account.name,
            "wxid": account.name.split("_")[0] if "_" in account.name else account.name,
            "db_count": len(db_files),
            "decrypted_count": len(decrypted),
            "size_mb": round(sum(f.stat().st_size for f in db_files) / 1048576, 1),
            "last_write": account.stat().st_mtime,
        })
    accounts.sort(key=lambda a: -a["last_write"])
    return accounts


def needs_decrypt(account: dict) -> bool:
    """账号是否还有未解密的 .db 文件。"""
    db_storage = Path(account["dir"]) / "db_storage"
    sources = {p.name: p for p in db_storage.rglob("*.db") if not p.name.endswith(".decrypted.db")}
    decrypted = {p.name: p for p in db_storage.rglob("*.decrypted.db")}
    for name, src in sources.items():
        dec = decrypted.get(name)
        if dec is None or dec.stat().st_mtime < src.stat().st_mtime:
            return True
    return False


CONFIG_CIPHER_NAME = b"com.Tencent.WCDB.Config.Cipher"
# Config.Cipher 密钥块自身用固定掩码异或，解出形如 x'<32B key><16B salt>' 的字面量
CONFIG_XOR_MASK = bytes.fromhex(
    "d2c7442458020000004889442450488b"
    "450048844c2448488944254048584c24"
)
_KEY_LITERAL_RE = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")


class _MemBasicInfo(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", ctypes.c_ulong),
        ("RegionSize", ctypes.c_size_t),
        ("State", ctypes.c_ulong),
        ("Protect", ctypes.c_ulong),
        ("Type", ctypes.c_ulong),
    ]


def _open_for_read(pid: int):
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = k32.OpenProcess(0x0010 | 0x0400, False, pid)
    if not handle:
        return None, None

    def read(addr: int, size: int):
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t(0)
        ok = k32.ReadProcessMemory(handle, ctypes.c_void_p(addr), buf, size,
                                   ctypes.byref(got))
        if ok and got.value:
            return buf.raw[:got.value]
        return None

    return handle, read


def _readable_regions(k32, handle) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    addr = 0
    info = _MemBasicInfo()
    readable = (0x02, 0x04, 0x20, 0x40, 0x80)
    while addr < 0x7FFFFFFFFFFF:
        if not k32.VirtualQueryEx(handle, ctypes.c_void_p(addr),
                                 ctypes.byref(info), ctypes.sizeof(info)):
            break
        base = info.BaseAddress or 0
        size = info.RegionSize or 0
        if size == 0:
            break
        nxt = base + size
        if info.State == 0x1000 and (info.Protect & 0xFF) in readable \
                and not (info.Protect & 0x100):
            out.append((base, size))
        addr = nxt if nxt > addr else addr + 0x1000
    return out


def _scan_memory(read, needle: bytes, regions) -> list[int]:
    hits: list[int] = []
    chunk = 4 * 1024 * 1024
    for base, size in regions:
        off = 0
        while off < size:
            n = min(chunk, size - off)
            data = read(base + off, n)
            if data:
                i = data.find(needle)
                while i >= 0:
                    hits.append(base + off + i)
                    i = data.find(needle, i + 1)
            off += n
    return hits


def _keys_from_config_cipher(read, regions) -> list[str]:
    """走 Config.Cipher 对象的引用链取出 x'<key><salt>' 密钥字面量。

    微信 4.1.x 起数据库密钥不再以裸 64 位 hex 存在内存，而是藏在
    com.Tencent.WCDB.Config.Cipher 配置对象里（固定掩码异或）。该路径
    不依赖 DLL 版本偏移，小版本升级后依然可用。
    """
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    needles = _scan_memory(read, CONFIG_CIPHER_NAME, regions)
    found: list[str] = []
    for needle_addr in needles[:32]:
        pair = struct.pack("<Q", needle_addr) + struct.pack("<Q", len(CONFIG_CIPHER_NAME))
        # 每个库一个引用节点，数量随库数增长，不能截断（截断会丢库密钥）
        for ref in _scan_memory(read, pair, regions):
            node = read(ref - 0x10, 0x50)
            if not node or len(node) < 0x40:
                continue
            if struct.unpack_from("<Q", node, 0x10)[0] != needle_addr:
                continue
            if struct.unpack_from("<Q", node, 0x18)[0] != len(CONFIG_CIPHER_NAME):
                continue
            config_ptr = struct.unpack_from("<Q", node, 0x28)[0]
            if not (0x10000 <= config_ptr < 0x800000000000):
                continue
            obj = read(config_ptr + 0x88, 0x28)
            if not obj or len(obj) < 0x18:
                continue
            data_ptr = struct.unpack_from("<Q", obj, 0x8)[0]
            data_len = struct.unpack_from("<Q", obj, 0x10)[0]
            if not (0 < data_len <= 1024 and 0x10000 <= data_ptr < 0x800000000000):
                continue
            blob = read(data_ptr, int(data_len))
            if not blob or len(blob) != data_len:
                continue
            decoded = bytes(b ^ CONFIG_XOR_MASK[i % len(CONFIG_XOR_MASK)]
                            for i, b in enumerate(blob))
            for m in _KEY_LITERAL_RE.finditer(decoded):
                run = m.group(1).decode().lower()
                if len(run) == 96:
                    found.append(run)          # key(32B) + salt(16B)
                elif len(run) == 64:
                    found.append(run)          # 裸 32B key
                else:
                    for s in range(0, len(run) - 63, 32):
                        if s + 96 <= len(run):
                            found.append(run[s:s + 96])
                        elif s + 64 <= len(run):
                            found.append(run[s:s + 64])
    del k32
    return found


def extract_keys(log=print) -> list[str]:
    """从运行中的 Weixin.exe 内存提取 SQLCipher 密钥。

    旧 64 位 hex 形式（微信 4.0.x）与 Config.Cipher 形式（4.1.x）都会尝试。
    返回 hex 字符串；96 位 hex 表示 key+salt（解密时按 48 字节处理）。
    """
    import psutil
    procs = [
        (p.pid, p.info["cmdline"])
        for p in psutil.process_iter(["pid", "name", "cmdline"])
        if p.info["name"] and p.info["name"].lower() == "weixin.exe"
    ]
    if not procs:
        log("未找到运行中的 Weixin.exe")
        return []
    pid = min(procs, key=lambda x: len(x[1] or []))[0]
    log(f"正在扫描 Weixin.exe (PID {pid}) 内存中的数据库密钥…")
    handle, read = _open_for_read(pid)
    if not handle:
        log("无法读取微信进程内存（可能需管理员权限）")
        return []
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        regions = _readable_regions(k32, handle)
        result: list[str] = []
        try:
            result.extend(_keys_from_config_cipher(read, regions))
            log(f"Config.Cipher 路径提取到 {len(result)} 个密钥字面量")
        except Exception as exc:
            log(f"Config.Cipher 提取失败：{exc}")
        if not result:
            # 回退：旧版 4.0.x 的 x'<64hex>' 裸密钥
            legacy = _scan_memory(read, b"x'", regions)
            for addr in legacy:
                b = read(addr, 67)
                if not b or len(b) < 67 or b[66:67] != b"'":
                    continue
                try:
                    hs = b[2:66].decode("ascii")
                except UnicodeDecodeError:
                    continue
                if re.fullmatch(r"[0-9a-fA-F]{64}", hs):
                    result.append(hs.lower())
            log(f"旧格式路径提取到 {len(result)} 个密钥")
        seen: dict[str, None] = {}
        for k in result:
            seen.setdefault(k, None)
        final = list(seen)
        log(f"共提取到 {len(final)} 个候选密钥")
        return final
    finally:
        try:
            ctypes.WinDLL("kernel32").CloseHandle(handle)
        except Exception:
            pass


def decrypt_db(db_path: Path, rawkey: bytes, out_path: Path | None = None) -> Path | None:
    """解密单个 SQLCipher 4 数据库，HMAC 校验失败返回 None。"""
    if out_path is None:
        out_path = db_path.with_name(db_path.stem + ".decrypted.db")

    # 48 字节 = 32B key + 16B 显式 salt（微信 4.1.x 的 x'<key><salt>' 形式）
    key = rawkey[:KEY_SZ]
    explicit_salt = rawkey[KEY_SZ:KEY_SZ + SALT_SZ] if len(rawkey) >= KEY_SZ + SALT_SZ else None

    # 先只读首页做 HMAC 校验：一次解密要试十几个候选密钥，读整库代价太大
    with open(db_path, "rb") as f:
        head = f.read(PAGE_SZ)
    if len(head) < PAGE_SZ:
        return None
    salt = explicit_salt or head[:SALT_SZ]
    page1 = head[SALT_SZ:PAGE_SZ]
    mac_salt = bytes(x ^ 0x3A for x in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", key, mac_salt, 2, KEY_SZ)
    digest = hmac.new(mac_key, digestmod="sha512")
    digest.update(page1[:-RESERVE_SZ + IV_SZ])
    digest.update(struct.pack("<I", 1))
    if not hmac.compare_digest(digest.digest(), page1[-RESERVE_SZ + IV_SZ:][:HMAC_SZ]):
        return None

    with open(db_path, "rb") as f:
        blist = f.read()
    pages = [page1]
    pages += [blist[i:i + PAGE_SZ] for i in range(PAGE_SZ, len(blist), PAGE_SZ)]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(SQLITE_HEADER)
        for page in pages:
            iv = page[-RESERVE_SZ:][:IV_SZ]
            dec = AES.new(key, AES.MODE_CBC, iv)
            f.write(dec.decrypt(page[:-RESERVE_SZ]))
            f.write(page[-RESERVE_SZ:])
    return out_path


def decrypt_account(account_dir: Path, target: Path, keys: list[bytes],
                    log=print) -> dict:
    """把单个账号 db_storage 下的数据库解密到 target（保持相对路径）。"""
    db_storage = account_dir / "db_storage"
    if not db_storage.is_dir():
        return {"decrypted": 0, "failed": 0}
    db_files = [p for p in db_storage.rglob("*.db")
                if not p.name.endswith(".decrypted.db")]
    log(f"发现 {len(db_files)} 个数据库文件")
    decrypted = failed = 0
    for db in db_files:
        rel = db.relative_to(db_storage)
        out = target / rel.with_name(rel.stem + ".decrypted.db")
        ok = False
        for raw in keys:
            if decrypt_db(db, raw, out):
                ok = True
                break
        if ok:
            decrypted += 1
        else:
            failed += 1
            log(f"  解密失败: {rel}")
    return {"decrypted": decrypted, "failed": failed}


def run_headless_decrypt(account_dir: str, target: str,
                         log_file: str = "", done_file: str = "",
                         log_cb=None) -> int:
    """扫描密钥并解密指定账号到 target 临时目录（无缓存，每次全新解密）。

    - 进程内调用：传 log_cb 回调即可在 UI 中显示进度。
    - 提权子进程调用：传 log_file/done_file 落盘供父进程轮询。
    """
    def log(msg: str):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        if log_cb is not None:
            try:
                log_cb(msg)
            except Exception:
                pass
        if log_file:
            try:
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass
    result = {"success": False, "decrypted": 0, "failed": 0, "keys": 0,
              "message": "", "finished": time.time()}
    try:
        log(f"开始解密：{account_dir}")
        keys = extract_keys(log)
        result["keys"] = len(keys)
        if not keys:
            result["message"] = "未提取到密钥，请确认微信正在运行且已登录。"
            raise RuntimeError(result["message"])
        account_path = Path(account_dir)
        if not account_path.is_dir():
            result["message"] = f"账号目录不存在：{account_dir}"
            raise RuntimeError(result["message"])
        target_path = Path(target)
        target_path.mkdir(parents=True, exist_ok=True)
        key_bytes = [bytes.fromhex(k) for k in keys]
        stats = decrypt_account(account_path, target_path, key_bytes, log=log)
        result["decrypted"] = stats["decrypted"]
        result["failed"] = stats["failed"]
        result["success"] = True
        result["message"] = f"完成：成功 {result['decrypted']}，失败 {result['failed']}"
        log(result["message"])
    except Exception as exc:
        result["message"] = f"解密出错：{exc}"
        try:
            log(result["message"])
        except Exception:
            pass
        if isinstance(exc, PermissionError):
            if done_file:
                try:
                    with open(done_file, "w", encoding="utf-8") as f:
                        json.dump(result, f, ensure_ascii=False)
                except Exception:
                    pass
            raise
    if done_file:
        try:
            with open(done_file, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False)
        except Exception:
            pass
    return 0 if result["success"] else 1


def elevate_and_decrypt(account_dir: Path, target: Path,
                        log_file: Path, done_file: Path) -> bool:
    """ShellExecute runas 提权启动本程序执行解密，返回是否成功发起。"""
    args = [sys.executable, "--decrypt", str(account_dir), str(target),
            str(log_file), str(done_file)]
    if sys.argv and sys.argv[0].endswith(".py"):
        # 源码运行时：python 脚本.py --decrypt ...
        args = [sys.executable, os.path.abspath(sys.argv[0]),
                "--decrypt", str(account_dir), str(target),
                str(log_file), str(done_file)]
    params = " ".join(f'"{a}"' for a in args)
    ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 0)
    return ret > 32


def log_paths() -> tuple[Path, Path]:
    tmp = Path(tempfile.gettempdir())
    return tmp / "wx_viewer_decrypt.log", tmp / "wx_viewer_done.json"
