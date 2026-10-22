# -*- coding: utf-8 -*-
"""聊天图片（V2 .dat）解密与显示。

微信 4.x 图片本地存储为 .dat：
    [6B 签名] [aes_size 4B LE] [xor_size 4B LE] [1B pad]
    [AES-128-ECB 区段] [明文区] [单字节 XOR 尾部]
密钥：aes_key = md5(f"{uin}{wxid}")[:16]（ASCII 16 字符），xor_key = uin & 0xFF。
旧版 V1（签名 07 08 56 31）用固定密钥 cfcd208495d565ef。
解密后可能是 JPEG / PNG / GIF / wxgf（HEVC 动图，ffmpeg 提取首帧）。
"""

from __future__ import annotations

import io
import os
import re
import struct
import threading
from pathlib import Path

from PIL import Image
from Crypto.Cipher import AES
from Crypto.Util import Padding

from emoticon_store import wxgf_to_png

import tkinter as tk

V2_MAGIC = b"\x07\x08\x56\x32\x08\x07"
V1_MAGIC = b"\x07\x08\x56\x31\x08\x07"
V1_KEY = b"cfcd208495d565ef"
IMG_MAGICS = (b"\x89PNG", b"GIF", b"\xff\xd8\xff", b"wxgf", b"RIFF")


def decrypt_dat(data: bytes, aes_key: bytes, xor_key: int) -> bytes | None:
    """V1/V2 .dat 解密，返回完整图片数据（可能为 wxgf）。"""
    if len(data) < 15:
        return None
    sig = data[:6]
    if sig == V2_MAGIC:
        key = aes_key
    elif sig == V1_MAGIC:
        key = V1_KEY
    else:
        return None
    aes_size, xor_size = struct.unpack_from("<LL", data, 6)
    if aes_size <= 0 or aes_size > 20 * 1024 * 1024:
        return None
    aligned = aes_size + (16 - aes_size % 16) if aes_size % 16 else aes_size + 16
    off = 15
    if off + aligned > len(data):
        return None
    try:
        dec = AES.new(key, AES.MODE_ECB).decrypt(data[off:off + aligned])
    except Exception:
        return None
    try:
        dec = Padding.unpad(dec, 16)
    except ValueError:
        pass
    raw_end = len(data) - xor_size
    if raw_end < off + aligned:
        raw_end = off + aligned
    raw = data[off + aligned:raw_end]
    tail = data[raw_end:]
    if tail and xor_key:
        tail = bytes(b ^ xor_key for b in tail)
    return dec + raw + tail


class ImageStore:
    """按消息路径提供聊天图片 PhotoImage，多级缓存 + wxgf 后台转帧。"""

    def __init__(self, data_dir, aes_key, xor_key, cache_dir,
                 log=print, max_workers=4, on_ready=None):
        self.attach_root = Path(data_dir) / "msg" / "attach"
        self.aes_key = aes_key.encode() if isinstance(aes_key, str) else bytes(aes_key)
        self.xor_key = int(xor_key)
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.on_ready = on_ready
        self._mem: dict[str, bytes] = {}
        self._photos: dict[str, tk.PhotoImage] = {}
        self._thumbs: dict[str, bytes] = {}
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._dl_slots = threading.Semaphore(max_workers)
        self._file_index: dict[str, str] | None = None
        self._ffmpeg: str | None = None

    # ---------- 文件定位 ----------
    def _build_index(self):
        """懒构建 {无后缀文件名: .dat 路径} 索引（缩略图优先原图）。"""
        if self._file_index is not None:
            return
        index: dict[str, str] = {}
        if self.attach_root.is_dir():
            for p in self.attach_root.rglob("*.dat"):
                name = p.name[:-4]
                base = name[:-2] if name.endswith(("_t", "_h")) else name
                # 优先原图；没有原图时保留缩略图
                if base not in index or not p.name.endswith("_t.dat"):
                    index[base] = str(p)
        self._file_index = index

    def _resolve_path(self, rel_path: str, md5: str = "") -> str | None:
        """按消息 path 或 md5 解析 .dat 绝对路径。"""
        if rel_path:
            # rel_path 形如 msg/attach/<会话>/<月>/Img/<fname>
            cand = self.attach_root.parent.parent / rel_path
            # 聊天气泡优先缩略图 _t（小图够用，且避免 wxgf 原图全白）；
            # 没有缩略图再回退原图。
            for suffix in ("_t", ""):
                p = cand.with_name(cand.name + suffix + ".dat")
                if p.is_file():
                    return str(p)
        self._build_index()
        if md5 and md5 in self._file_index:
            return self._file_index[md5]
        return None

    # ---------- 解密 ----------
    def _decrypt_file(self, path: str) -> bytes | None:
        try:
            data = Path(path).read_bytes()
        except OSError:
            return None
        out = decrypt_dat(data, self.aes_key, self.xor_key)
        if not out:
            return None
        if out[:4] == b"wxgf":
            png = wxgf_to_png(out, self._ffmpeg or None)
            if self._ffmpeg is None:
                import imageio_ffmpeg
                try:
                    self._ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
                except Exception:
                    self._ffmpeg = ""
            if not png:
                return None
            return png
        return out

    # ---------- 取图 ----------
    def _save_cache(self, key: str, data: bytes):
        ext = "png"
        if data[:4] == b"\x89PNG":
            ext = "png"
        elif data[:3] == b"GIF":
            ext = "gif"
        elif data[:3] == b"\xff\xd8\xff":
            ext = "jpg"
        try:
            (self.cache_dir / f"{key}.{ext}").write_bytes(data)
        except OSError:
            pass

    def _make_thumb(self, data: bytes, max_w: int, max_h: int) -> bytes | None:
        """后台线程：把图片缩放为缩略图 PNG bytes（不碰 Tk）。"""
        try:
            img = Image.open(io.BytesIO(data))
            img = img.convert("RGBA")
        except Exception:
            return None
        w, h = img.size
        if w > max_w or h > max_h:
            scale = min(max_w / w, max_h / h)
            img = img.resize((max(1, int(w * scale)),
                              max(1, int(h * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        try:
            img.save(buf, format="PNG")
        except Exception:
            return None
        return buf.getvalue()

    def get_bytes(self, rel_path: str = "", md5: str = "") -> bytes | None:
        key = (rel_path or md5 or "").strip().lower()
        if not key:
            return None
        with self._lock:
            if key in self._mem:
                return self._mem[key]
        hits = sorted(self.cache_dir.glob(os.path.basename(key) + ".*"))
        if hits:
            try:
                data = hits[0].read_bytes()
            except OSError:
                data = None
            if data:
                with self._lock:
                    self._mem[key] = data
                return data
        path = self._resolve_path(rel_path, md5)
        if not path:
            return None
        data = self._decrypt_file(path)
        if not data:
            return None
        with self._lock:
            self._mem[key] = data
        self._save_cache(os.path.basename(key), data)
        return data

    def get_bytes_hd(self, rel_path: str = "", md5: str = "") -> bytes | None:
        """大图查看：优先取 _h 高清图（正常 PNG），其次缩略图，最后原图。

        原图可能是 wxgf 动图（首帧空白），thumbnail 转帧较慢，
        大图场景优先 _h 可获得完整清晰画面且不白屏。
        """
        key = (rel_path or md5 or "").strip().lower()
        if not key:
            return None
        if rel_path:
            cand = self.attach_root.parent.parent / rel_path
            for suffix in ("_h", "_t", ""):
                p = cand.with_name(cand.name + suffix + ".dat")
                if p.is_file():
                    data = self._decrypt_file(str(p))
                    if data:
                        return data
        return self.get_bytes(rel_path, md5)

    def get_photo(self, rel_path: str = "", md5: str = "",
                  max_w: int = 240, max_h: int = 240):
        """仅从缓存取图（不触发同步解密），缺失返回 None 由后台补。"""
        key = (rel_path or md5 or "").strip().lower()
        if not key:
            return None
        with self._lock:
            if key in self._photos:
                return self._photos[key]
        with self._lock:
            thumb = self._thumbs.get(key)
        if thumb is None:
            hits = sorted(self.cache_dir.glob(os.path.basename(key) + ".*"))
            if hits:
                try:
                    data = hits[0].read_bytes()
                except OSError:
                    data = None
                if data:
                    thumb = self._make_thumb(data, max_w, max_h)
                    if thumb:
                        with self._lock:
                            self._thumbs[key] = thumb
        if thumb is None:
            return None
        return self._to_photo(key, thumb)

    def _to_photo(self, key: str, thumb: bytes):
        """主线程：直接由缩略图 PNG bytes 创建 PhotoImage。"""
        try:
            photo = tk.PhotoImage(data=thumb)
        except Exception:
            return None
        with self._lock:
            self._photos[key] = photo
        return photo

    # ---------- 异步（后台解密/转帧，不阻塞 UI） ----------
    def is_pending(self, key: str) -> bool:
        key = (key or "").strip().lower()
        with self._lock:
            return key in self._pending or key in self._photos

    def mark_pending(self, key: str):
        with self._lock:
            self._pending.add((key or "").strip().lower())

    def fetch_async(self, rel_path: str = "", md5: str = ""):
        key = (rel_path or md5 or "").strip().lower()
        if not key:
            return

        def _worker():
            with self._dl_slots:
                try:
                    data = self.get_bytes(rel_path, md5)
                    ok = bool(data)
                except Exception:
                    ok, data = False, None
                finally:
                    if ok and data:
                        thumb = self._make_thumb(data, 240, 240)
                        if thumb:
                            with self._lock:
                                self._thumbs[key] = thumb
                    with self._lock:
                        self._pending.discard(key)
                    if ok and self.on_ready:
                        try:
                            self.on_ready(key)
                        except Exception:
                            pass
        threading.Thread(target=_worker, daemon=True).start()
