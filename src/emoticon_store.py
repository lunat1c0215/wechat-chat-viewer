# -*- coding: utf-8 -*-
"""表情资源加载：本地解密（Thumb/Persist）+ CDN 下载 + wxgf 首帧转码 + 缓存。

微信 4.x 表情文件存储于 business/emoticon 下，文件名 = 内容 md5，加密方式：
    AES-128-CBC + PKCS7，key = IV = md5(f"{seed}{wxid}EMOTICON") 前 16 字节。
Thumb 目录（*.thumb）解密后为静态 PNG/JPG，适合聊天展示；
Persist 目录解密后可能是 wxgf（HEVC 动图），用 ffmpeg 提取首帧；
本地没有的表情，可用消息 XML 里的 cdnurl 直接下载明文图片。
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import tempfile
import threading
import urllib.request
from pathlib import Path

from PIL import Image
from Crypto.Cipher import AES

import tkinter as tk


def _fmt(data: bytes) -> str:
    if data[:4] == b"\x89PNG":
        return "png"
    if data[:3] == b"GIF":
        return "gif"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"wxgf":
        return "wxgf"
    if data[:4] == b"RIFF":
        return "webp"
    return ""


def wxgf_to_png(data: bytes, ffmpeg_path: str | None = None) -> bytes | None:
    """wxgf（微信 HEVC 动图）→ 有内容帧 PNG。

    微信动图首帧常为空白占位帧，直接取首帧会得到全白图；
    用 ffmpeg thumbnail 滤镜挑信息量最大的帧，避免全白。
    ffmpeg_path 复用避免重复探测。
    """
    idx = data.find(b"\x00\x00\x00\x01\x40\x01")
    hevc = data[idx:] if idx > 0 else data
    try:
        if ffmpeg_path is None:
            import imageio_ffmpeg
            ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "in.hevc")
            dst = os.path.join(td, "out.png")
            with open(src, "wb") as f:
                f.write(hevc)
            subprocess.run(
                [ffmpeg_path, "-y", "-i", src, "-frames:v", "1",
                 "-vf", "thumbnail=24", "-update", "1", dst],
                capture_output=True, timeout=40, creationflags=0x08000000)
            if os.path.exists(dst):
                with open(dst, "rb") as f:
                    return f.read()
    except Exception:
        pass
    return None


class EmoticonStore:
    """按 md5 提供表情图片（PhotoImage），多级取图并做磁盘缓存。"""

    def __init__(self, emoji_dir, key, cache_dir, log=print, max_workers=6,
                 on_ready=None, db_urls=None):
        self.emoji_dir = Path(emoji_dir)
        self.key = bytes.fromhex(key) if isinstance(key, str) else bytes(key)
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.on_ready = on_ready
        # md5 -> (aes_key_hex, cdn_url, encrypt_url)，来自 emoticon.db 收藏表
        self.db_urls: dict[str, tuple] = db_urls or {}
        self._mem: dict[str, bytes] = {}
        self._photos: dict[str, tk.PhotoImage] = {}
        self._thumbs: dict[str, bytes] = {}
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._dl_slots = threading.Semaphore(max_workers)
        self._ffmpeg: str | None = None

    # ---------- 本地解密 ----------
    def _local_candidates(self, md5: str) -> list[Path]:
        return [
            self.emoji_dir / "Thumb" / md5[:2] / (md5 + ".thumb"),
            self.emoji_dir / "Persist" / md5[:2] / md5,
        ]

    def _decrypt_file(self, path: Path) -> bytes | None:
        try:
            C = path.read_bytes()
        except OSError:
            return None
        try:
            dec = AES.new(self.key, AES.MODE_CBC, self.key).decrypt(C)
        except Exception:
            return None
        pad = dec[-1]
        if not (0 < pad <= 16 and all(b == pad for b in dec[-pad:])):
            return None
        return dec[:-pad]

    # ---------- CDN 下载 ----------
    def _download(self, cdnurl: str) -> bytes | None:
        try:
            req = urllib.request.Request(
                cdnurl, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                return resp.read()
        except Exception:
            return None

    # ---------- wxgf 首帧 ----------
    def _wxgf_to_png(self, data: bytes) -> bytes | None:
        if self._ffmpeg is None:
            import imageio_ffmpeg
            try:
                self._ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                self._ffmpeg = ""
        return wxgf_to_png(data, self._ffmpeg or None)

    # ---------- 主取图 ----------
    def _save_cache(self, md5: str, data: bytes):
        ext = _fmt(data) or "dat"
        try:
            (self.cache_dir / f"{md5}.{ext}").write_bytes(data)
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

    def get_bytes(self, md5: str, cdnurl: str = "",
                  allow_download: bool = True) -> bytes | None:
        md5 = (md5 or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{32}", md5):
            return None
        with self._lock:
            if md5 in self._mem:
                return self._mem[md5]
        hits = sorted(self.cache_dir.glob(md5 + ".*"))
        if hits:
            try:
                data = hits[0].read_bytes()
            except OSError:
                data = None
            if data:
                with self._lock:
                    self._mem[md5] = data
                return data
        for p in self._local_candidates(md5):
            if p.is_file():
                dec = self._decrypt_file(p)
                if dec and _fmt(dec):
                    if dec[:4] == b"wxgf":
                        png = self._wxgf_to_png(dec)
                        if not png:
                            continue
                        dec = png
                    with self._lock:
                        self._mem[md5] = dec
                    self._save_cache(md5, dec)
                    return dec
        # emoticon.db 收藏表：cdn_url 明文 / encrypt_url 用 aes_key 解密
        row = self.db_urls.get(md5)
        if allow_download and row:
            aes_hex, cdn, enc = row
            data = self._download(cdn) if cdn else None
            if not data and enc and aes_hex:
                raw = self._download(enc)
                if raw:
                    try:
                        k = bytes.fromhex(aes_hex)
                        if len(k) == 16:
                            data = AES.new(k, AES.MODE_CBC, k).decrypt(raw)
                    except Exception:
                        data = None
            if data and _fmt(data):
                if data[:4] == b"wxgf":
                    png = self._wxgf_to_png(data)
                    data = png if png else None
                if data:
                    with self._lock:
                        self._mem[md5] = data
                    self._save_cache(md5, data)
                    return data
        if allow_download and cdnurl:
            data = self._download(cdnurl)
            if data and _fmt(data):
                if data[:4] == b"wxgf":
                    png = self._wxgf_to_png(data)
                    data = png if png else None
                if data:
                    with self._lock:
                        self._mem[md5] = data
                    self._save_cache(md5, data)
                    return data
        return None

    def get_photo(self, md5: str, cdnurl: str = "",
                  allow_download: bool = True,
                  max_w: int = 160, max_h: int = 160):
        md5 = (md5 or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{32}", md5):
            return None
        with self._lock:
            if md5 in self._photos:
                return self._photos[md5]
        with self._lock:
            thumb = self._thumbs.get(md5)
        if thumb is not None:
            return self._to_photo(md5, thumb)
        data = self.get_bytes(md5, cdnurl, allow_download)
        if not data:
            return None
        if data[:4] == b"wxgf":
            # 主线程不执行 ffmpeg 转码，交给后台 fetch_async
            if not allow_download:
                return None
            png = self._wxgf_to_png(data)
            if not png:
                return None
            data = png
        thumb = self._make_thumb(data, max_w, max_h)
        if not thumb:
            return None
        with self._lock:
            self._thumbs[md5] = thumb
        return self._to_photo(md5, thumb)

    def _to_photo(self, md5: str, thumb: bytes):
        try:
            photo = tk.PhotoImage(data=thumb)
        except Exception:
            return None
        with self._lock:
            self._photos[md5] = photo
        return photo

    # ---------- 异步下载（UI 不阻塞） ----------
    def is_pending(self, md5: str) -> bool:
        md5 = (md5 or "").strip().lower()
        with self._lock:
            return md5 in self._pending or md5 in self._photos

    def mark_pending(self, md5: str):
        md5 = (md5 or "").strip().lower()
        with self._lock:
            self._pending.add(md5)

    def fetch_async(self, md5: str, cdnurl: str = ""):
        """后台线程：下载并缓存缺失表情（并发受信号量限制）。"""
        def _worker():
            with self._dl_slots:
                try:
                    data = self.get_bytes(md5, cdnurl, allow_download=True)
                    ok = bool(data)
                except Exception:
                    ok, data = False, None
                finally:
                    if ok and data and data[:4] != b"wxgf":
                        thumb = self._make_thumb(data, 160, 160)
                        if thumb:
                            with self._lock:
                                self._thumbs[md5] = thumb
                    with self._lock:
                        self._pending.discard(md5)
                    if ok and self.on_ready:
                        try:
                            self.on_ready(md5)
                        except Exception:
                            pass
        threading.Thread(target=_worker, daemon=True).start()
