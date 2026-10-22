#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微信聊天记录查看器（微信 4.x 本地数据，跨电脑可用版）。

启动即自动检测本机微信与数据，未解密时弹一次管理员授权自动完成
密钥提取 + 数据库解密，随后进入气泡式聊天查看器。
"""

from __future__ import annotations

import csv
import ctypes
import hashlib
import io
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image

# 降低 GIL 切换间隔：后台线程（内存扫描/解密）不再饿死主线程，
# 保证加载期间界面持续响应（进度条、日志、窗口不卡死）。
try:
    sys.setswitchinterval(0.001)
except Exception:
    pass

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

import ui_kit  # noqa: E402
from ui_kit import GButton, SlimProgress  # noqa: E402
from ui_kit import _vgrad as _ui_vgrad  # noqa: E402


# ================= Frutiger Aero（收敛版配色） =================
# 规则：全局只保留一个高饱和主色（主蓝），其余全部降饱和；
# 绿色只用于"自己"的气泡，红色只用于取消/错误。
THEME = {
    "title_top": "#4C93F3",       # 标题栏渐变顶
    "title_bottom": "#2456B8",    # 标题栏渐变底
    "title_text": "#FFFFFF",
    "title_sub": "#DCE9FF",
    "bg_top": "#E9F2FE",          # 页面背景
    "bg_bottom": "#F4F9FF",       # 聊天区底色（更浅，避免刺眼）
    "card": "#FFFFFF",
    "card_border": "#DCE6F5",
    "card_shadow": "#D7E3F4",
    "divider": "#E3EBF7",
    "accent": "#2F6FE0",          # 主蓝：唯一高饱和色
    "accent_light": "#6BA3FF",
    "accent_dark": "#2455B5",
    "frutiger_green": "#0FBFC5",
    "bubble_self_top": "#8FE8B0",
    "bubble_self_bottom": "#46C97B",
    "bubble_self_border": "#3DB96E",
    "bubble_other": "#FFFFFF",
    "bubble_other_bottom": "#F3F8FE",
    "bubble_border": "#D8E3F2",
    "text_main": "#1B2A41",
    "text_sub": "#5E6E86",
    "text_weak": "#8FA0B6",
    "text_white": "#FFFFFF",
    "ok": "#17A34A",
    "warn": "#C98A12",
    "err": "#D2453C",
    "xphighlight": "#2F6FE0",
    "btn_top": "#FFFFFF",
    "btn_bottom": "#EDF4FD",
    "btn_border_hi": "#FFFFFF",
    "btn_border_lo": "#B9CCE4",
    "tree_bg": "#FFFFFF",
    "tree_sel": "#2F6FE0",
    "tree_hover": "#EAF2FE",
    "skeleton": "#E4EDF8",
    "skeleton_hi": "#EFF5FD",
}


def make_vgrad(w: int, h: int, c1: str, c2: str) -> Image.Image:
    """兼容旧接口：垂直渐变图片（Pillow，1px 列拉伸）。"""
    return _ui_vgrad(max(1, w), max(1, h), c1, c2)


def _shade(hex_color: str, amount: float) -> str:
    """调亮/调暗一个颜色：amount>0 变亮，<0 变暗。"""
    c = hex_color.lstrip("#")
    r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    if amount >= 0:
        r = int(r + (255 - r) * amount)
        g = int(g + (255 - g) * amount)
        b = int(b + (255 - b) * amount)
    else:
        r = int(r * (1 + amount))
        g = int(g * (1 + amount))
        b = int(b * (1 + amount))
    return "#%02x%02x%02x" % (max(0, min(255, r)), max(0, min(255, g)),
                              max(0, min(255, b)))


def vgrad_photo(w: int, h: int, c1: str, c2: str) -> tk.PhotoImage:
    return ui_kit.gradient_photo(w, h, c1, c2)


def get_grad(w: int, h: int, c1: str, c2: str,
             noise: int = 0, gloss: bool = False) -> tk.PhotoImage:
    """带缓存的渐变（可选噪点 / 玻璃高光）。"""
    return ui_kit.gradient_photo(w, h, c1, c2, noise=noise, gloss=gloss)

from db_service import DBService  # noqa: E402
from emoticon_store import EmoticonStore  # noqa: E402
from image_store import ImageStore  # noqa: E402
from models import (  # noqa: E402
    MSG_TYPE_ANIMATION, MSG_TYPE_CARD, MSG_TYPE_IMAGE, MSG_TYPE_LOCATION,
    MSG_TYPE_FILE, MSG_TYPE_SHARE, MSG_TYPE_SYSTEM, MSG_TYPE_TEXT, MSG_TYPE_VIDEO,
    MSG_TYPE_VOICE, MSG_TYPE_VOIP, talker_to_table_name,
)
import wxdecrypt  # noqa: E402

APP_NAME = "微信聊天记录查看器"
TYPE_LABELS = {
    MSG_TYPE_TEXT: "文本", MSG_TYPE_IMAGE: "图片", MSG_TYPE_VOICE: "语音",
    MSG_TYPE_VIDEO: "视频", MSG_TYPE_ANIMATION: "表情", MSG_TYPE_LOCATION: "位置",
    MSG_TYPE_SHARE: "分享/链接", MSG_TYPE_VOIP: "通话", MSG_TYPE_SYSTEM: "系统",
    MSG_TYPE_CARD: "名片",
    5: "链接", 6: "文件", 8: "文件", 19: "合并转发", 33: "小程序",
    51: "视频号", 53: "接龙", 57: "引用", 62: "拍了拍", 2000: "转账", 2001: "红包",
}


def pretty_content(m) -> str:
    """把消息内容转成可读文本（含类型前缀），用于气泡与详情展示。"""
    label = TYPE_LABELS.get(m.type, f"类型{m.type}")
    content = (m.content or "").strip()
    if not content.startswith("<"):
        if content.startswith("["):
            return f"[{label}]"
        if m.type == MSG_TYPE_TEXT:
            return content or "(空消息)"
        return f"[{label}] {content}"
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return f"[{label}]"

    def find_text(*paths):
        for path in paths:
            el = root.find(path)
            if el is not None and el.text and el.text.strip():
                return el.text.strip()
        return ""

    title = find_text(".//title")
    des = find_text(".//des")
    t = m.type
    if t == 62:
        return title or "[拍了拍]"
    if t == 2000:
        return f"微信转账：{des or title or ''}" or f"[{label}]"
    if t == 2001:
        return f"红包：{des or '给你发了一个红包'}"
    if t == 19:
        brief = re.sub(r"\s+", " ", des)[:90] if des else ""
        return f"[{label}] {title}：{brief}".rstrip("：")
    if title:
        return f"[{label}] {title}"
    if des:
        return f"[{label}] {des}"
    return f"[{label}]"


def wrap_lines(text: str, font, max_px: int) -> list[str]:
    """按像素宽度逐字符换行（兼容中文与英文混排）。"""
    if not text:
        return [""]
    result: list[str] = []
    for para in str(text).split("\n"):
        if not para:
            result.append("")
            continue
        cur = ""
        for ch in para:
            if cur and font.measure(cur + ch) > max_px:
                result.append(cur)
                cur = ch
            else:
                cur += ch
        result.append(cur)
    return result


class AvatarStore:
    """头像缓存：head_image 库 BLOB -> 40x40 PhotoImage，缺省生成首字头像。"""

    def __init__(self, size: int = 40):
        self.size = size
        self.images: dict = {}
        self._default_png: dict = {}
        self._default_photo: dict = {}
        self._font = None

    def load_async(self, db_storage: Path, event_queue, usernames: list[str],
                   on_done=None, progress_cb=None):
        self._usernames = usernames
        threading.Thread(target=self._worker,
                         args=(db_storage, event_queue, on_done, progress_cb),
                         daemon=True).start()

    def _worker(self, db_storage: Path, event_queue, on_done=None,
                progress_cb=None):
        blobs: dict = {}
        head_db = db_storage / "head_image" / "head_image.decrypted.db"
        if head_db.exists():
            try:
                conn = sqlite3.connect(str(head_db))
                cur = conn.cursor()
                cur.execute("SELECT username, image_buffer FROM head_image")
                for username, blob in cur.fetchall():
                    if blob:
                        blobs[username] = bytes(blob)
                conn.close()
            except Exception:
                pass
        result: dict = {}
        usernames = set(self._usernames)
        for i, username in enumerate(usernames):
            blob = blobs.get(username)
            if blob:
                png = self._to_png_bytes(blob, username)
                if png:
                    result[username] = png
            if progress_cb and (i % 20 == 0 or i == len(usernames) - 1):
                progress_cb(4.0 * (i + 1) / max(1, len(usernames)))
        event_queue.put(("avatars", result))
        if on_done:
            try:
                on_done()
            except Exception:
                pass

    def _to_png_bytes(self, blob: bytes, username: str) -> bytes | None:
        try:
            img = Image.open(io.BytesIO(blob)).convert("RGB")
            img = img.resize((self.size, self.size), Image.BILINEAR)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        except Exception:
            return None

    def _default_font(self):
        if self._font is None:
            from PIL import ImageFont
            try:
                self._font = ImageFont.truetype("msyh.ttc",
                                                int(self.size * 0.55))
            except Exception:
                self._font = ImageFont.load_default()
        return self._font

    def make_default(self, username: str, name: str = "") -> bytes:
        """彩色圆形 + 首字默认头像；按「颜色+首字」缓存 PNG，避免重复绘制。"""
        s = self.size
        palette = ["#4A90D9", "#5CB85C", "#F0AD4E", "#D9534F", "#8E6BB8",
                   "#3FB6B2", "#E8838A", "#7A8BA6"]
        h = int(hashlib.md5((username or "x").encode()).hexdigest(), 16)
        color = palette[h % len(palette)]
        ch = (name or "?").strip()[:1]
        key = (s, color, ch)
        cached = self._default_png.get(key)
        if cached is not None:
            return cached
        img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        from PIL import ImageDraw
        draw = ImageDraw.Draw(img)
        draw.ellipse((0, 0, s - 1, s - 1), fill=color)
        font = self._default_font()
        bbox = draw.textbbox((0, 0), ch, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text(((s - tw) / 2 - bbox[0], (s - th) / 2 - bbox[1]), ch,
                  font=font, fill="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()
        self._default_png[key] = data
        return data

    def apply(self, mapping: dict):
        """主线程：把 PNG bytes 转成 PhotoImage。"""
        for username, png in mapping.items():
            try:
                self.images[username] = tk.PhotoImage(data=png)
            except Exception:
                pass

    def ensure(self, username: str, name: str = ""):
        if username in self.images:
            return
        try:
            data = self.make_default(username, name)
            photo = self._default_photo.get(data)
            if photo is None:
                photo = tk.PhotoImage(data=data)
                self._default_photo[data] = photo
            self.images[username] = photo
        except Exception:
            pass

    def get(self, username: str):
        return self.images.get(username)


class ImageViewer(tk.Toplevel):
    """大图查看窗口：点击聊天图片/表情后弹出，支持适应窗口、滚轮缩放、
    拖拽平移、双击还原、Esc/× 关闭。
    """

    def __init__(self, master, data: bytes, title: str = "图片"):
        super().__init__(master)
        self.title(title)
        self.configure(bg="#101014")
        self.minsize(320, 240)
        _place_dialog(self, 860, 640, master)
        self._data = data
        self._img: Image.Image | None = None
        self._scale = 1.0
        self._ox = 0.0
        self._oy = 0.0
        self._drag = None
        self._photo = None
        self._item = None
        self._fit_first = True
        self._render_job = None
        self._thumb_cache: dict = {}

        self.canvas = tk.Canvas(self, bg="#101014", highlightthickness=0,
                                cursor="fleur")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Double-Button-1>", self._on_double)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Configure>", self._on_resize)
        self.bind("<Escape>", lambda e: self.destroy())

        bar = tk.Frame(self, bg="#1b1b22")
        bar.pack(fill="x")
        self.zoom_label = tk.Label(bar, text="100%", bg="#1b1b22",
                                   fg="#9aa0a6", font=("Microsoft YaHei", 9))
        self.zoom_label.pack(side="left", padx=10, pady=6)
        self.size_label = tk.Label(bar, text="", bg="#1b1b22",
                                   fg="#9aa0a6", font=("Microsoft YaHei", 9))
        self.size_label.pack(side="left", padx=4)
        tk.Button(bar, text="适应窗口", command=self._fit,
                  bg="#2a2a33", fg="#e8eaed", activebackground="#3a3a45",
                  relief="flat", font=("Microsoft YaHei", 9),
                  cursor="hand2").pack(side="right", padx=6, pady=4)
        tk.Button(bar, text="100%", command=self._zoom_100,
                  bg="#2a2a33", fg="#e8eaed", activebackground="#3a3a45",
                  relief="flat", font=("Microsoft YaHei", 9),
                  cursor="hand2").pack(side="right", padx=2, pady=4)

        threading.Thread(target=self._load, daemon=True).start()
        self.after(50, self._check_loaded)

    # ---------- 加载 ----------
    def _load(self):
        try:
            img = Image.open(io.BytesIO(self._data))
            img.load()
            self._img = img
        except Exception:
            self._img = None

    def _check_loaded(self):
        if self._img is None:
            # 尚未解码完成，稍后再查
            if not self.winfo_exists():
                return
            self.after(50, self._check_loaded)
            return
        if not self.winfo_exists():
            return
        self.size_label.configure(
            text=f"{self._img.width} × {self._img.height}")
        self._fit()

    # ---------- 绘制 ----------
    def _render(self):
        if self._img is None:
            return
        w = max(1, int(self._img.width * self._scale))
        h = max(1, int(self._img.height * self._scale))
        key = (w, h)
        thumb = self._thumb_cache.get(key)
        if thumb is None:
            if len(self._thumb_cache) > 12:
                self._thumb_cache.clear()
            try:
                thumb_img = self._img.resize((w, h), Image.LANCZOS)
                buf = io.BytesIO()
                thumb_img.save(buf, format="PNG")
                thumb = buf.getvalue()
                self._thumb_cache[key] = thumb
            except Exception:
                return
        try:
            self._photo = tk.PhotoImage(data=thumb)
        except Exception:
            return
        cw = max(self.canvas.winfo_width(), 10)
        ch = max(self.canvas.winfo_height(), 10)
        x = cw / 2 + self._ox - self._photo.width() / 2
        y = ch / 2 + self._oy - self._photo.height() / 2
        self.canvas.delete("all")
        self._item = self.canvas.create_image(x, y, image=self._photo,
                                              anchor="nw")
        self.zoom_label.configure(text=f"{int(self._scale * 100)}%")

    def _fit(self):
        if self._img is None:
            return
        cw = max(self.canvas.winfo_width(), 100)
        ch = max(self.canvas.winfo_height(), 100)
        pad = 24
        self._scale = min((cw - pad) / self._img.width,
                          (ch - pad) / self._img.height)
        self._scale = max(0.02, min(self._scale, 8.0))
        self._ox = self._oy = 0.0
        self._render()

    def _zoom_100(self):
        self._scale = 1.0
        self._ox = self._oy = 0.0
        self._render()

    # ---------- 交互 ----------
    def _on_press(self, event):
        self._drag = (event.x_root, event.y_root, self._ox, self._oy)

    def _on_drag(self, event):
        if not self._drag:
            return
        px, py, ox, oy = self._drag
        self._ox = ox + (event.x_root - px)
        self._oy = oy + (event.y_root - py)
        if self._item is not None:
            cw = max(self.canvas.winfo_width(), 10)
            ch = max(self.canvas.winfo_height(), 10)
            x = cw / 2 + self._ox - self._photo.width() / 2
            y = ch / 2 + self._oy - self._photo.height() / 2
            self.canvas.coords(self._item, x, y)

    def _on_release(self, _event):
        self._drag = None

    def _on_double(self, _event):
        if self._fit_first:
            self._zoom_100()
            self._fit_first = False
        else:
            self._fit()
            self._fit_first = True

    def _on_wheel(self, event):
        if self._img is None:
            return
        factor = 1.15 if event.delta > 0 else 1 / 1.15
        self._scale = max(0.02, min(self._scale * factor, 12.0))
        # 缩放防抖：快速滚动只重绘最后一次，避免反复重采样大图卡顿
        if self._render_job is not None:
            try:
                self.after_cancel(self._render_job)
            except Exception:
                pass
        self._render_job = self.after(40, self._render)

    def _on_resize(self, _event):
        if self._img is not None and self._fit_first:
            self._render()


class ChatViewer(tk.Tk):
    def __init__(self, selftest: bool = False):
        super().__init__()
        # 高 DPI 下限制 Tk 的字体放大倍数，避免按像素写死的布局被挤破
        try:
            cur = float(self.tk.call("tk", "scaling"))
            if cur > 1.35:
                self.tk.call("tk", "scaling", 1.35)
        except Exception:
            pass
        self.title(APP_NAME)
        _place_main_window(self, 1180, 720)
        self.minsize(900, 560)
        self.configure(bg=THEME["bg_bottom"])

        self.data_dir: Path | None = None
        self.svc: DBService | None = None
        self.accounts: list[dict] = []
        self.current_root: Path = wxdecrypt.default_xwechat_root()
        self.current_account: dict | None = None
        self.temp_data: Path | None = None
        self.current_talker: str | None = None
        self.current_msgs: list = []
        self.sessions: list = []
        self.session_counts: dict = {}
        self.sort_mode = "time"
        self.sort_order = "desc"
        self.loading = False
        self.decrypting = False
        self.waiting = False
        self.state = "boot"            # boot/detect/ready/work/error/idle
        self.detected: dict | None = None
        self.detect_version = ""
        self.detect_total = 0
        self._cancel_event = threading.Event()
        self._load_cancel = False
        self.event_queue: queue.Queue = queue.Queue()
        self.avatar_store = AvatarStore(40)
        self.emoticon_store: EmoticonStore | None = None
        self.image_store: ImageStore | None = None
        self._emoji_setup_started = False
        self._emoji_done_event = threading.Event()
        self._rerender_job = None
        self._load_target = 0
        self._load_emoticons = True
        self._rendered_talker = None
        self._sess_job = None
        self._render_limit = 150
        self._keep_view = False
        self.selftest = selftest
        self._marks = {}

        self._configure_style()
        self.page_home = tk.Frame(self, bg=THEME["bg_bottom"])
        self.page_viewer = tk.Frame(self, bg=THEME["bg_bottom"])
        self._build_home()
        self._build_viewer()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Control-f>", lambda e: self.search_entry.focus_set())
        self._show_home()
        self.after(100, self._poll_events)
        self.after(60, self._auto_start)

    def _mark(self, name: str):
        """记录阶段耗时（自检 / 回归用）。"""
        try:
            base = _APP_T0
        except NameError:
            base = time.monotonic()
        self._marks[name] = round(time.monotonic() - base, 2)

    def _selftest_finish(self):
        """自检：写出阶段耗时与加载结果，然后自动退出。"""
        self._write_marks()
        if os.environ.get("WX_SELFTEST_HOLD"):
            self.after(300, self._selftest_first_session)
        else:
            self.after(400, self._on_close)

    def _write_marks(self):
        import json
        data = dict(self._marks)
        data["sessions"] = len(self.sessions)
        data["messages"] = len(self.current_msgs)
        data["temp_data"] = str(self.temp_data or "")
        try:
            out = Path(tempfile.gettempdir()) / "wx_selftest.json"
            out.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            print(json.dumps(data, ensure_ascii=False))
        except Exception:
            pass

    def _selftest_first_session(self):
        """自检：自动打开第一个有消息的会话，用于截图核对气泡样式。"""
        try:
            best = None
            for node in self.session_tree.get_children(""):
                kids = self.session_tree.get_children(node)
                if kids:
                    best = kids[0] if best is None else best
                    self.session_tree.item(node, open=True)
            if best:
                self.session_tree.selection_set(best)
                self.session_tree.see(best)
        except Exception:
            pass

    def _on_close(self):
        """退出：先关连接，再把临时目录交给独立清理进程（窗口瞬间消失，不卡顿）。"""
        if getattr(self, "_closing", False):
            return
        self._closing = True
        try:
            _save_window_box(self)
        except Exception:
            pass
        # 内存 seed 扫描在独立进程里跑，父进程退出前必须收掉，否则会留个吃 CPU 的孤儿
        try:
            wxdecrypt.kill_seed_scans()
        except Exception:
            pass
        self.waiting = False
        self._cancel_event.set()
        temp = self.temp_data
        self.temp_data = None
        if self.svc is not None:
            try:
                self.svc.close()
            except Exception:
                pass
            self.svc = None
        try:
            self.withdraw()
            self.update_idletasks()
        except Exception:
            pass
        if temp and temp.is_dir():
            self._spawn_cleanup(temp)
        try:
            self.destroy()
        except Exception:
            pass
        # 后台还在扫描内存的守护线程不该拖住退出：临时目录已交给清理进程，
        # 这里直接结束进程，保证点关闭后立刻真正退出。
        os._exit(0)

    @staticmethod
    def _spawn_cleanup(path: Path):
        """用带 --cleanup 的独立进程删除临时目录：不等删完，窗口立即关闭。"""
        try:
            args = [sys.executable]
            if not getattr(sys, "frozen", False):
                args.append(str(Path(__file__).resolve()))
            args += ["--cleanup", str(path)]
            subprocess.Popen(args, close_fds=True,
                             creationflags=0x00000008 | 0x08000000)
            return
        except Exception:
            pass
        try:
            threading.Thread(target=shutil.rmtree, args=(path, True),
                             daemon=True).start()
        except Exception:
            pass

    def _poll_events(self):
        try:
            try:
                while True:
                    kind, payload = self.event_queue.get_nowait()
                    try:
                        self._dispatch_event(kind, payload)
                    except Exception:
                        # 单个事件处理失败不能中断主循环，记日志继续
                        import traceback
                        traceback.print_exc()
            except queue.Empty:
                pass
        finally:
            # 无论上面发生什么，都必须续上下一次轮询，否则界面永远不再刷新
            try:
                self.after(100, self._poll_events)
            except Exception:
                pass

    def _dispatch_event(self, kind, payload):
        """事件分发（独立方法，便于单事件异常隔离）。"""
        if kind == "wechat_detected":
            account, version, total = payload
            self._on_wechat_detected(account, version, total)
        elif kind == "detect_hint":
            if self.state == "detect":
                self.home_sub.configure(text=payload)
                self._wizard_log(payload, "")
        elif kind == "detect_stopped":
            pass
        elif kind == "work_progress":
            pct, text = payload
            self.home_progress.set(pct)
            self.home_pct.configure(text=f"{int(pct)}%")
            if self.state == "work":
                self.home_sub.configure(text=text)
        elif kind == "load_progress":
            pct, text = payload
            self._load_target = pct
            try:
                self.load_progress.set(pct)
                self.load_pct.configure(text=f"{int(pct)}%")
                self.load_stage.configure(text=text)
                self.status_var.set(text)
            except Exception:
                pass
        elif kind == "sessions_loaded":
            self.sessions = payload or []
            self.session_counts = {}
            self._schedule_sessions_render()
        elif kind == "load_done":
            self._hide_loading_overlay()
            self._mark("load_done")
            if self.current_account:
                name = self.current_account.get("name", "")
                self.status_var.set(
                    f"已加载：{name}，{len(self.sessions)} 个会话"
                    "（临时数据，退出后清除）")
            if self.selftest:
                self._selftest_finish()
        elif kind == "load_error":
            self._hide_loading_overlay()
            messagebox.showerror(APP_NAME, f"数据库加载失败：\n{payload}")
            self.status_var.set("加载失败")
            if self.selftest:
                self._marks["load_error"] = str(payload)
                self._selftest_finish()
        elif kind == "sessions_counts":
            self.session_counts = payload or {}
            self._schedule_sessions_render()
        elif kind == "avatars":
            self.avatar_store.apply(payload or {})
            for s in self.sessions:
                self.avatar_store.ensure(s.username, s.nick_name or "")
            self._schedule_sessions_render()
            if self.current_msgs:
                self._rerender_page()
        elif kind == "emoticon_ready":
            self._schedule_rerender()
        elif kind == "image_ready":
            # 图片库就绪后强制重绘，把此前的 [图片] 占位符刷新为真实图片
            self._schedule_rerender(force=True)
        elif kind == "msgs_loaded":
            talker, msgs, total = payload
            self._close_work_progress()
            self._mark("messages_loaded")
            if talker == self.current_talker:
                _t0 = time.monotonic()
                self.render_messages(msgs)
                self._marks["render_ms"] = int((time.monotonic() - _t0) * 1000)
                if self.selftest:
                    self._write_marks()
                name = self.svc.get_contact_display(talker)
                self.chat_header.configure(text=f"{name}（{total} 条）")
                self.status_var.set(f"{name}：{total} 条消息")
            self.loading = False
        elif kind == "msgs_load_error":
            self._close_work_progress()
            messagebox.showerror(APP_NAME, f"读取消息失败：\n{payload}")
            self.status_var.set("读取失败")
            self.loading = False
        elif kind == "export_done":
            self._close_work_progress()
            messagebox.showinfo(APP_NAME, f"已导出到：\n{payload}")
        elif kind == "export_error":
            self._close_work_progress()
            messagebox.showerror(APP_NAME, f"导出失败：\n{payload}")
        elif kind == "stats_done":
            progress, bar, lines = payload
            self._show_stats_result(progress, bar, lines)
        elif kind == "stats_error":
            progress, bar, msg = payload
            self._show_stats_error(progress, bar, msg)
        elif kind == "log":
            self._wizard_log(payload, "")
        elif kind == "decrypt_done":
            self._after_decrypt(True)
        elif kind == "decrypt_failed":
            self._decrypt_failed(payload)
        elif kind == "decrypt_cancelled":
            self._decrypt_cancelled()

    def _schedule_rerender(self, force: bool = False):
        """合并多次图片/表情就绪事件为一次重绘，避免重绘风暴卡死 UI。

        force=True 时立即重绘（图片库刚就绪，用户看到的全是占位符）。
        否则用 250ms 防抖：连续多张图片完成只重绘一次。
        """
        if force:
            self._rerender_job = None
            if self.current_msgs:
                self._rerender_page()
            return
        if not self.current_msgs:
            return
        if getattr(self, "_rerender_job", None) is not None:
            return
        self._rerender_job = self.after(250, self._flush_rerender)

    def _schedule_sessions_render(self):
        """合并连续的列表刷新（会话列表 / 计数 / 头像）为一次渲染。"""
        if getattr(self, "_sess_job", None) is not None:
            return
        self._sess_job = self.after(60, self._flush_sessions_render)

    def _flush_sessions_render(self):
        self._sess_job = None
        self.render_sessions(self.sessions)

    def _flush_rerender(self):
        self._rerender_job = None
        if self.current_msgs:
            self._rerender_page()

    def _rerender_page(self):
        """图片/表情就绪、窗口缩放时重绘当前会话。"""
        if self.current_msgs:
            self._keep_view = True
            try:
                self.render_messages(self.current_msgs)
            finally:
                self._keep_view = False

    # ---------- 样式 ----------
    def _configure_style(self):
        style = ttk.Style(self)
        T = THEME
        try:
            style.theme_use("clam")  # clam 支持自定义配色
        except tk.TclError:
            pass
        self.configure(bg=T["bg_bottom"])

        # 会话树：行高 48，选中为主蓝，无 3D 边框
        style.configure("Treeview",
                        rowheight=48,
                        font=("Microsoft YaHei", 10),
                        background=T["tree_bg"],
                        fieldbackground=T["tree_bg"],
                        foreground=T["text_main"],
                        borderwidth=0)
        style.map("Treeview",
                  background=[("selected", T["tree_sel"])],
                  foreground=[("selected", "#FFFFFF")])
        style.configure("Treeview.Heading",
                        font=("Microsoft YaHei", 9),
                        background=T["card"],
                        foreground=T["text_weak"],
                        relief="flat",
                        borderwidth=0,
                        padding=(6, 4))
        style.map("Treeview.Heading",
                  background=[("active", T["card"])],
                  relief=[("active", "flat")])

        # 按钮：扁平浅蓝（ghost），主操作用 GButton
        style.configure("TButton",
                        font=("Microsoft YaHei", 9),
                        background=T["btn_top"],
                        foreground=T["text_main"],
                        bordercolor="#C9D8EC",
                        lightcolor=T["btn_top"],
                        darkcolor="#C9D8EC",
                        relief="flat",
                        padding=(9, 4),
                        borderwidth=1)
        style.map("TButton",
                  background=[("active", T["btn_bottom"]),
                              ("pressed", "#DCE9F8")],
                  bordercolor=[("active", T["accent_light"])])
        style.configure("Tool.TButton", font=("Microsoft YaHei", 9))
        # 带主色文字的次级按钮（统计 / 导出）
        style.configure("Accent.TButton",
                        font=("Microsoft YaHei", 9),
                        foreground=T["accent"],
                        background="#F4F9FF",
                        bordercolor="#CFE0F5",
                        lightcolor="#F4F9FF",
                        darkcolor="#CFE0F5",
                        relief="flat",
                        borderwidth=1,
                        padding=(9, 4))
        style.map("Accent.TButton",
                  foreground=[("active", T["accent_dark"])],
                  background=[("active", "#E7F1FE"), ("pressed", "#DCEAFC")],
                  bordercolor=[("active", T["accent_light"])])

        # 输入框 / 下拉框
        style.configure("TEntry",
                        fieldbackground="#FFFFFF",
                        foreground=T["text_main"],
                        bordercolor="#C9D8EC",
                        lightcolor="#FFFFFF",
                        darkcolor="#C9D8EC",
                        insertcolor=T["accent"],
                        relief="flat",
                        padding=(4, 4))
        style.map("TEntry", bordercolor=[("focus", T["accent_light"])])
        style.configure("TCombobox",
                        fieldbackground="#FFFFFF",
                        foreground=T["text_main"],
                        background=T["btn_top"],
                        bordercolor="#C9D8EC",
                        arrowcolor=T["text_sub"],
                        relief="flat",
                        padding=(4, 3))
        style.map("TCombobox",
                  fieldbackground=[("readonly", "#FFFFFF")],
                  bordercolor=[("focus", T["accent_light"])])

        # 细滚动条
        style.configure("Vertical.TScrollbar",
                        gripcount=0,
                        background="#CFDEF1",
                        troughcolor=T["bg_bottom"],
                        bordercolor=T["bg_bottom"],
                        lightcolor="#CFDEF1",
                        darkcolor="#CFDEF1",
                        arrowcolor=T["text_weak"],
                        arrowsize=10,
                        width=10)
        style.map("Vertical.TScrollbar",
                  background=[("active", "#B6CCE9")])
        style.configure("Horizontal.TScrollbar",
                        gripcount=0,
                        background="#CFDEF1",
                        troughcolor=T["bg_bottom"],
                        bordercolor=T["bg_bottom"],
                        arrowcolor=T["text_weak"],
                        arrowsize=10,
                        width=10)
        # 统计等后台任务的进度条
        style.configure("Horizontal.TProgressbar",
                        background=T["accent"],
                        troughcolor=T["bg_top"],
                        bordercolor=T["bg_top"],
                        lightcolor=T["accent_light"],
                        darkcolor=T["accent"],
                        thickness=8)

    # ================= 启动页：检测 → 确认 → 解密（一张卡片走完） =================
    def _build_title_bar(self, parent, subtitle="", height=58):
        """玻璃标题栏：Canvas 绘制渐变 + 文字，避免 Label 底色割裂渐变。"""
        T = THEME
        bar = tk.Canvas(parent, height=height, bd=0, highlightthickness=0,
                        bg=T["title_bottom"])
        bar.pack(fill="x", side="top")
        bar._bg_w = 1400
        bar._photo = get_grad(bar._bg_w, height, T["title_top"],
                              T["title_bottom"], gloss=True)
        bar._bg_item = bar.create_image(0, 0, anchor="nw", image=bar._photo)
        bar._title_id = bar.create_text(
            22, height / 2, text=APP_NAME, anchor="w", fill=T["title_text"],
            font=("Microsoft YaHei", 15, "bold"))
        bar._sub_id = bar.create_text(
            22 + 236, height / 2 + 1, text=subtitle, anchor="w",
            fill=T["title_sub"], font=("Microsoft YaHei", 9))

        def _on_cfg(ev):
            if ev.width < 80 or abs(ev.width - bar._bg_w) < 3:
                return
            bar._bg_w = ev.width
            bar._photo = get_grad(ev.width, height, T["title_top"],
                                  T["title_bottom"], gloss=True)
            bar.itemconfigure(bar._bg_item, image=bar._photo)

        bar.bind("<Configure>", _on_cfg)
        return bar

    def _build_home(self):
        T = THEME
        page = self.page_home

        # 背景：极轻噪点渐变（铺满，先创建 → 处于最底层）
        self.home_bg = tk.Label(
            page, image=get_grad(1280, 760, T["bg_top"], T["bg_bottom"], noise=1),
            bd=0)
        self.home_bg.place(x=0, y=0, relwidth=1, relheight=1)

        self._build_title_bar(page, "本地读取 · 无缓存 · 退出即清除")

        holder = tk.Frame(page, bg=T["bg_top"])
        holder.place(relx=0.5, rely=0.47, anchor="center")

        card = tk.Frame(holder, bg=T["card"], padx=30, pady=24,
                        highlightthickness=1,
                        highlightbackground=T["card_border"])
        card.pack()
        # 卡片下的 3px 浅色投影，替代无法做模糊的 Tk 阴影
        tk.Frame(holder, bg=T["card_shadow"], height=3).pack(fill="x", padx=7)

        tk.Frame(card, bg=T["card"], width=560, height=0).pack()

        head = tk.Frame(card, bg=T["card"])
        head.pack(fill="x")
        self.state_dot = tk.Canvas(head, width=14, height=14, bg=T["card"],
                                   highlightthickness=0, bd=0)
        self.state_dot.pack(side="left", padx=(0, 9), pady=(5, 0))
        self._dot = self.state_dot.create_oval(3, 3, 13, 13,
                                               fill=T["accent_light"], outline="")
        self.home_title = tk.Label(head, text="正在检测微信…", bg=T["card"],
                                   fg=T["text_main"], anchor="w",
                                   font=("Microsoft YaHei", 17, "bold"))
        self.home_title.pack(side="left")
        self.home_elapsed = tk.Label(head, text="", bg=T["card"],
                                     fg=T["text_weak"], anchor="e",
                                     font=("Microsoft YaHei", 9))
        self.home_elapsed.pack(side="right", pady=(6, 0))

        self.home_sub = tk.Label(
            card, text="已开始自动检测，请确保电脑版微信已登录。",
            bg=T["card"], fg=T["text_sub"], anchor="w", justify="left",
            wraplength=560, font=("Microsoft YaHei", 10))
        self.home_sub.pack(fill="x", pady=(9, 0))

        tk.Frame(card, bg=T["divider"], height=1).pack(fill="x", pady=16)

        acc = tk.Frame(card, bg=T["card"])
        acc.pack(fill="x")
        self.home_avatar = AvatarStore(56)
        self.home_avatar.ensure("__detecting__", "微")
        self.acc_avatar = tk.Label(acc, bg=T["card"], bd=0,
                                   image=self.home_avatar.get("__detecting__"))
        self.acc_avatar.pack(side="left", padx=(0, 14))
        acc_r = tk.Frame(acc, bg=T["card"])
        acc_r.pack(side="left", fill="x", expand=True)
        self.acc_name = tk.Label(acc_r, text="正在识别微信号…", bg=T["card"],
                                 fg=T["text_main"], anchor="w",
                                 font=("Microsoft YaHei", 13, "bold"))
        self.acc_name.pack(fill="x")
        self.acc_meta = tk.Label(acc_r, text="检测中…", bg=T["card"],
                                 fg=T["text_sub"], anchor="w",
                                 font=("Microsoft YaHei", 9))
        self.acc_meta.pack(fill="x", pady=(4, 0))

        opt = tk.Frame(card, bg=T["card"])
        opt.pack(fill="x", pady=(16, 0))
        self._emoji_opt_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            opt, text="加载表情图片（约慢 2 秒，可看到动态表情）",
            variable=self._emoji_opt_var, bg=T["card"],
            activebackground=T["card"], selectcolor="#FFFFFF",
            fg=T["text_main"], activeforeground=T["text_main"],
            font=("Microsoft YaHei", 9), bd=0, highlightthickness=0,
            anchor="w", cursor="hand2").pack(anchor="w")

        prog_row = tk.Frame(card, bg=T["card"])
        prog_row.pack(fill="x", pady=(18, 0))
        self.home_progress = SlimProgress(
            prog_row, width=500, height=10, bg=T["card"],
            track="#E4EDF9", fill=T["accent"], light=T["accent_light"])
        self.home_progress.pack(side="left", fill="x", expand=True)
        self.home_pct = tk.Label(prog_row, text="", bg=T["card"],
                                 fg=T["text_sub"], anchor="e", width=5,
                                 font=("Microsoft YaHei", 9))
        self.home_pct.pack(side="right", padx=(12, 0))

        btns = tk.Frame(card, bg=T["card"])
        btns.pack(fill="x", pady=(20, 0))
        self.btn_enter = GButton(btns, "进入", command=self._ui_enter,
                                 kind="primary", width=122, height=38,
                                 font=("Microsoft YaHei", 10, "bold"))
        self.btn_enter.pack(side="left")
        self.btn_cancel = GButton(btns, "取消", command=self._ui_cancel,
                                  kind="ghost", width=94, height=38)
        self.btn_cancel.pack(side="left", padx=(10, 0))
        self.btn_retry = GButton(btns, "重新检测", command=self._ui_retry,
                                 kind="ghost", width=108, height=38)
        self.btn_retry.pack(side="left", padx=(10, 0))
        self.link_pick = tk.Label(btns, text="选择数据目录", bg=T["card"],
                                  fg=T["accent"], cursor="hand2",
                                  font=("Microsoft YaHei", 9, "underline"))
        self.link_pick.pack(side="right", pady=(10, 0))
        self.link_pick.bind("<Button-1>", lambda e: self.pick_root())

        self.detail_link = tk.Label(card, text="查看详情", bg=T["card"],
                                    fg=T["text_weak"], cursor="hand2",
                                    anchor="w", font=("Microsoft YaHei", 9))
        self.detail_link.pack(fill="x", pady=(16, 0))
        self.detail_link.bind("<Button-1>", lambda e: self._toggle_details())
        self.detail_frame = tk.Frame(card, bg=T["card"])
        self.wizard_text = tk.Text(
            self.detail_frame, height=7, wrap="word", font=("Consolas", 9),
            bg="#F8FBFF", fg=T["text_sub"], bd=0, highlightthickness=1,
            highlightbackground=T["divider"], padx=8, pady=6)
        self.wizard_text.pack(fill="both", expand=True)
        self.wizard_text.tag_configure("ok", foreground=T["ok"])
        self.wizard_text.tag_configure("warn", foreground=T["warn"])
        self.wizard_text.tag_configure("err", foreground=T["err"])
        self.wizard_text.tag_configure("head", foreground=T["text_main"],
                                       font=("Microsoft YaHei", 9, "bold"))
        self.wizard_text.configure(state="disabled")
        self._details_open = False

        tk.Label(page, text="本地读取 · 不联网上传 · 退出后自动清除临时数据",
                 bg=T["bg_top"], fg=T["text_weak"],
                 font=("Microsoft YaHei", 9)).place(
                     relx=0.5, rely=0.965, anchor="center")

    # ---------- 启动状态机 ----------
    def _set_state(self, state, title=None, sub=None):
        """统一切换启动页状态：按钮可用性、指示灯颜色、进度条模式。"""
        T = THEME
        self.state = state
        dot = {"detect": T["accent_light"], "ready": T["ok"], "work": T["accent"],
               "error": T["err"], "idle": T["text_weak"]}.get(state, T["accent"])
        try:
            self.state_dot.itemconfigure(self._dot, fill=dot)
        except Exception:
            pass
        if title:
            self.home_title.configure(text=title)
        if sub is not None:
            self.home_sub.configure(text=sub)

        if state == "detect":
            self.btn_enter.set_enabled(False)
            self.btn_retry.set_enabled(True)
            self.btn_retry.set_text("重新检测")
            self.btn_cancel.set_enabled(True)
            self.btn_cancel.set_text("取消")
            self.home_progress.stop_crawl()
            self.home_progress.start_crawl(4, 26, 0.35)
            self.home_pct.configure(text="")
        elif state == "ready":
            self.btn_enter.set_enabled(True)
            self.btn_retry.set_enabled(True)
            self.btn_retry.set_text("重新检测")
            self.btn_cancel.set_enabled(True)
            self.btn_cancel.set_text("取消")
            self.home_progress.stop_crawl()
            self.home_progress.set(0)
            self.home_pct.configure(text="")
        elif state == "work":
            self.btn_enter.set_enabled(False)
            self.btn_retry.set_enabled(False)
            self.btn_cancel.set_enabled(True)
            self.btn_cancel.set_text("取消")
        elif state == "error":
            self.btn_enter.set_enabled(bool(self.detected))
            self.btn_retry.set_enabled(True)
            self.btn_retry.set_text("重试")
            self.btn_cancel.set_enabled(True)
            self.btn_cancel.set_text("退出")
            self.home_progress.stop_crawl()
            self.home_progress.set(0)
            self.home_pct.configure(text="")
        elif state == "idle":
            self.btn_enter.set_enabled(False)
            self.btn_retry.set_enabled(True)
            self.btn_retry.set_text("立即检测")
            self.btn_cancel.set_enabled(False)
            self.home_progress.stop_crawl()
            self.home_progress.set(0)
            self.home_pct.configure(text="")
        self._tick_elapsed()

    def _tick_elapsed(self):
        job = getattr(self, "_elapsed_job", None)
        if job:
            try:
                self.after_cancel(job)
            except Exception:
                pass
            self._elapsed_job = None
        if self.state not in ("detect", "work"):
            self.home_elapsed.configure(text="")
            return
        elapsed = int(time.monotonic() - getattr(self, "_state_t0",
                                                 time.monotonic()))
        self.home_elapsed.configure(text=f"{elapsed} 秒")
        self._elapsed_job = self.after(500, self._tick_elapsed)

    def _toggle_details(self):
        self._details_open = not self._details_open
        if self._details_open:
            self.detail_frame.pack(fill="x", pady=(8, 0))
            self.detail_link.configure(text="收起详情")
        else:
            self.detail_frame.pack_forget()
            self.detail_link.configure(text="查看详情")

    def _wizard_log(self, text, tag=None):
        try:
            self.wizard_text.configure(state="normal")
            self.wizard_text.insert("end", text + "\n", tag or ())
            self.wizard_text.see("end")
            self.wizard_text.configure(state="disabled")
        except Exception:
            pass

    def _show_home(self):
        self.page_viewer.pack_forget()
        self.page_home.pack(fill="both", expand=True)

    def _show_viewer(self):
        self.page_home.pack_forget()
        self.page_viewer.pack(fill="both", expand=True)

    def _reset_home_card(self):
        self.home_avatar.images.pop("__detecting__", None)
        self.home_avatar.ensure("__detecting__", "微")
        self.acc_avatar.configure(image=self.home_avatar.get("__detecting__"))
        self.acc_name.configure(text="正在识别微信号…")
        self.acc_meta.configure(text="检测中…")

    def _auto_start(self, manual=False):
        if self.waiting or self.decrypting:
            return
        self._cancel_event.clear()
        self.waiting = True
        self.detected = None
        self.current_account = None
        self._state_t0 = time.monotonic()
        self._reset_home_card()
        self._set_state(
            "detect", "正在检测微信…",
            "请打开并登录电脑版微信。识别到账号后，这张卡片会直接出现“进入”。")
        self._wizard_log("开始检测微信进程…", "head")
        self.threading_wait_worker = threading.Thread(
            target=self._wait_worker, daemon=True)
        self.threading_wait_worker.start()

    def _wait_worker(self):
        """轮询检测：微信进程 + 账号数据目录，命中后交给主线程。"""
        tick = 0
        last_hint = ""
        while not self._cancel_event.is_set():
            proc = wxdecrypt.find_weixin_process()
            if proc:
                accounts = wxdecrypt.list_accounts(self.current_root)
                if accounts:
                    version = wxdecrypt.wechat_version(proc)
                    self.event_queue.put(
                        ("wechat_detected", (accounts[0], version, len(accounts))))
                    return
                hint = "已发现微信进程，正在等待登录后的数据目录…"
            else:
                hint = "未检测到微信进程，请先打开并登录电脑版微信。"
            if hint != last_hint:
                last_hint = hint
                self.event_queue.put(("detect_hint", hint))
            tick += 1
            self._cancel_event.wait(1.2)
        self.event_queue.put(("detect_stopped", None))

    def _on_wechat_detected(self, account, version, total=1):
        self.waiting = False
        self._mark("detected")
        self.detected = account
        self.detect_version = version or "未知"
        self.detect_total = total
        name = account["name"]
        self.home_avatar.images.pop(name, None)
        self.home_avatar.ensure(name, name.split("_")[0][:1].upper() or "微")
        self.acc_avatar.configure(image=self.home_avatar.get(name))
        self.acc_name.configure(text=name)
        extra = f"　·　本机共 {total} 个账号" if total > 1 else ""
        self.acc_meta.configure(
            text=f"微信 {self.detect_version}　·　{account['db_count']} 个数据库"
                 f"　·　约 {account['size_mb']} MB{extra}")
        self._set_state(
            "ready", "已检测到微信",
            "确认上面这个微信号是要查看的吗？点“进入”后会自动完成密钥提取、"
            "解密与加载，中途可随时取消。")
        self._wizard_log(f"侦测到：{name}（微信 {self.detect_version}）", "ok")
        self._wizard_log(f"数据目录：{account['dir']}", "")
        if self.selftest:
            self.after(150, self._ui_enter)

    def _ui_enter(self):
        if not self.detected or self.decrypting or self.waiting:
            return
        self._load_emoticons = bool(self._emoji_opt_var.get())
        self.start_decrypt(self.detected)

    def _ui_retry(self):
        if self.decrypting or self.waiting:
            return
        self.detected = None
        self._auto_start()

    def _ui_cancel(self):
        if self.state == "work":
            self._cancel_event.set()
            self.btn_cancel.set_enabled(False)
            self.home_sub.configure(text="正在取消并清理临时文件…")
        elif self.state == "detect":
            self.waiting = False
            self._cancel_event.set()
            self._set_state("idle", "已取消检测",
                            "点击“立即检测”可以重新开始。")
        elif self.state == "ready":
            self.detected = None
            self._reset_home_card()
            self._set_state("idle", "已取消",
                            "点击“立即检测”重新识别微信号。")
        else:
            self._on_close()

    def pick_root(self):
        initial = str(self.current_root)
        selected = filedialog.askdirectory(
            title="选择 xwechat_files 数据根目录", initialdir=initial)
        if not selected:
            return
        self.current_root = Path(selected)
        self.waiting = False
        self._cancel_event.set()
        self.detected = None
        self.after(120, self._auto_start)

    def start_decrypt(self, account):
        if self.decrypting:
            return
        self.decrypting = True
        self.waiting = False
        self._cancel_event.clear()
        self._load_cancel = False
        self.detected = account
        self.current_account = account
        if self.svc is not None:
            try:
                self.svc.close()
            except Exception:
                pass
            self.svc = None
        if self.temp_data and self.temp_data.is_dir():
            old = self.temp_data
            threading.Thread(target=shutil.rmtree, args=(old, True),
                             daemon=True).start()
        self.temp_data = Path(tempfile.mkdtemp(prefix="wx_viewer_"))
        self._mark("decrypt_start")
        self._state_t0 = time.monotonic()
        self._set_state(
            "work", "正在读取密钥…",
            "从微信进程内存提取数据库密钥（不会修改微信数据），随后逐个解密数据库。")
        self.home_progress.stop_crawl()
        self.home_progress.set(3)
        self.home_pct.configure(text="3%")
        self._wizard_log("开始提取密钥并解密（临时目录，退出后自动清除）", "head")
        threading.Thread(target=self._decrypt_worker, args=(account,),
                         daemon=True).start()

    def _decrypt_worker(self, account):
        target = self.temp_data / "db_storage"
        result: dict = {}

        def log_cb(msg):
            self.event_queue.put(("log", msg))

        def prog(pct, text):
            self.event_queue.put(("work_progress", (pct, text)))

        try:
            code = wxdecrypt.run_headless_decrypt(
                str(account["dir"]), str(target), log_cb=log_cb,
                progress_cb=prog, cancel_event=self._cancel_event,
                result_out=result)
        except PermissionError:
            self.event_queue.put(("log", "普通权限读取被拒绝，改用管理员方式重试…"))
            self._elevate_fallback(target)
            return
        if code == 0:
            self.event_queue.put(("decrypt_done", None))
        elif code == 2:
            self.event_queue.put(("decrypt_cancelled", None))
        else:
            self.event_queue.put(
                ("decrypt_failed", result.get("message") or "解密未完全成功，请重试。"))

    def _elevate_fallback(self, target: Path):
        log_file, done_file = wxdecrypt.log_paths()
        for p in (log_file, done_file):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        self.event_queue.put(("log", "即将弹出系统授权（UAC），请点“是”。"))
        account = self.current_account
        ok = wxdecrypt.elevate_and_decrypt(Path(account["dir"]), target,
                                           log_file, done_file)
        if not ok:
            self.event_queue.put(
                ("decrypt_failed", "无法启动管理员授权，请手动以管理员身份运行。"))
            return
        deadline = time.monotonic() + 240
        last_len = 0
        while time.monotonic() < deadline:
            if self._cancel_event.is_set():
                self.event_queue.put(("decrypt_cancelled", None))
                return
            time.sleep(0.6)
            if log_file.exists():
                text = log_file.read_text(encoding="utf-8", errors="replace")
                if len(text) > last_len:
                    new = text[last_len:]
                    last_len = len(text)
                    self.event_queue.put(("log", new.strip()))
            if done_file.exists():
                try:
                    result = json_safe_load(done_file)
                except Exception:
                    result = None
                if result:
                    kind = "decrypt_done" if result.get("success") else "decrypt_failed"
                    self.event_queue.put(
                        (kind, result.get("message") or "解密失败"))
                    return
        self.event_queue.put(("decrypt_failed", "解密超时，请重试。"))

    def _after_decrypt(self, success):
        self.decrypting = False
        self._mark("decrypt_done")
        ready = []
        if self.temp_data:
            ready = list((self.temp_data / "db_storage").rglob("*.decrypted.db"))
        if not success or not ready:
            self._decrypt_failed("未能解密出可用数据，请确认微信已登录后重试。")
            return
        self.home_progress.set(100)
        self.home_pct.configure(text="100%")
        self._wizard_log(f"解密完成（{len(ready)} 个数据库），正在进入查看器…", "ok")
        self.enter_viewer()

    def _decrypt_failed(self, msg):
        self.decrypting = False
        hint = ""
        text = str(msg or "")
        if "解密出错" in text:
            text = "读取密钥或解密过程中出错，请重试。"
            hint = " 若持续失败，可展开“查看详情”再反馈。"
        elif "权限" in text or "拒绝" in text:
            hint = " 可右键“以管理员身份运行”后重试。"
        elif "密钥" in text:
            hint = " 请确认电脑版微信已登录，并停留在主界面。"
        elif "数据目录" in text or "不存在" in text:
            hint = " 可点右下角“选择数据目录”手动指定 xwechat_files。"
        self._wizard_log(f"失败：{msg}", "err")
        self._set_state("error", "没能完成解密", f"{msg}{hint}")

    def _decrypt_cancelled(self):
        self.decrypting = False
        if self.temp_data and self.temp_data.is_dir():
            old = self.temp_data
            threading.Thread(target=shutil.rmtree, args=(old, True),
                             daemon=True).start()
            self.temp_data = None
        self._set_state("ready", "已取消",
                        "已停止解密并清理临时文件，可以重新点“进入”。")

    # ================= 查看器页 =================
    def _build_viewer(self):
        T = THEME
        self._view_title = self._build_title_bar(self.page_viewer, "正在读取数据…")

        bar = tk.Frame(self.page_viewer, bg=T["card"], padx=12, pady=8,
                       highlightthickness=1,
                       highlightbackground=T["card_border"])
        bar.pack(fill="x")
        self.search_var = tk.StringVar()
        search_entry = ttk.Entry(bar, textvariable=self.search_var, width=26)
        search_entry.pack(side="left", padx=(0, 6), ipady=1)
        search_entry.bind("<Return>", lambda e: self.search_messages())
        self.search_entry = search_entry
        GButton(bar, "搜索", command=self.search_messages, kind="primary",
                width=72, height=30,
                font=("Microsoft YaHei", 9)).pack(side="left")
        ttk.Button(bar, text="清除", command=self.clear_search).pack(side="left", padx=(4, 0))
        tk.Frame(bar, bg=T["divider"], width=1, height=18).pack(
            side="left", padx=9, pady=2)
        tk.Label(bar, text="时间", bg=T["card"], fg=T["text_sub"],
                 font=("Microsoft YaHei", 9)).pack(side="left")
        self.date_from = ttk.Entry(bar, width=11)
        self.date_from.pack(side="left", padx=(6, 3))
        tk.Label(bar, text="至", bg=T["card"], fg=T["text_sub"]).pack(side="left")
        self.date_to = ttk.Entry(bar, width=11)
        self.date_to.pack(side="left", padx=(3, 3))
        ttk.Button(bar, text="筛选", command=self.reload_current).pack(side="left", padx=(4, 0))
        ttk.Button(bar, text="重置", command=self.reset_filters).pack(side="left", padx=(4, 0))
        pick = tk.Label(bar, text="选择数据目录", bg=T["card"], fg=T["text_weak"],
                        cursor="hand2", font=("Microsoft YaHei", 9, "underline"))
        pick.pack(side="right")
        pick.bind("<Button-1>", lambda e: self.pick_root())
        ttk.Button(bar, text="导出当前会话", style="Accent.TButton",
                   command=self.export_current).pack(side="right", padx=(0, 10))
        ttk.Button(bar, text="统计", style="Accent.TButton",
                   command=self.show_stats).pack(side="right", padx=(0, 6))

        body = tk.Frame(self.page_viewer, bg=T["bg_bottom"])
        body.pack(fill="both", expand=True, padx=10, pady=(10, 6))
        self.body_frame = body

        left = tk.Frame(body, bg=T["card"], width=330,
                        highlightthickness=1,
                        highlightbackground=T["card_border"])
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        head = tk.Frame(left, bg=T["card"])
        head.pack(fill="x", padx=10, pady=(6, 4))
        tk.Label(head, text="✉ 会话列表", bg=T["card"], fg=T["accent"],
                 font=("Microsoft YaHei", 11, "bold"), anchor="w").pack(side="left")
        self.order_combo = ttk.Combobox(head, state="readonly", width=5,
                                        values=("倒序", "正序"), font=("Microsoft YaHei", 9))
        self.order_combo.set("倒序")
        self.order_combo.pack(side="right", padx=(2, 0))
        tk.Label(head, text="顺序", bg=T["card"], fg=T["text_sub"],
                 font=("Microsoft YaHei", 9)).pack(side="right")
        self.sort_combo = ttk.Combobox(head, state="readonly", width=8,
                                       values=("时间", "数量"), font=("Microsoft YaHei", 9))
        self.sort_combo.set("时间")
        self.sort_combo.pack(side="right", padx=(2, 0))
        tk.Label(head, text="排序", bg=T["card"], fg=T["text_sub"],
                 font=("Microsoft YaHei", 9)).pack(side="right")
        self.sort_combo.bind("<<ComboboxSelected>>", self.on_sort_change)
        self.order_combo.bind("<<ComboboxSelected>>", self.on_sort_change)
        self.session_search = ttk.Entry(left)
        self.session_search.pack(fill="x", padx=8, pady=(0, 6))
        self._session_search_ph = "搜索会话…"
        self._add_placeholder(self.session_search, self._session_search_ph)
        self.session_search.bind("<KeyRelease>", lambda e: self.filter_sessions())
        self.session_search.bind("<Return>", lambda e: self.filter_sessions())
        tree_frame = tk.Frame(left, bg=T["card"])
        tree_frame.pack(fill="both", expand=True)
        self.session_tree = ttk.Treeview(tree_frame, columns=("count", "time"),
                                         show="tree", selectmode="browse")
        self.session_tree.heading("#0", text="会话")
        self.session_tree.column("#0", width=170, anchor="w")
        self.session_tree.heading("count", text="条数")
        self.session_tree.column("count", width=52, anchor="e", stretch=False)
        self.session_tree.heading("time", text="最近")
        self.session_tree.column("time", width=92, anchor="center", stretch=False)
        vs = ttk.Scrollbar(tree_frame, orient="vertical", command=self.session_tree.yview)
        self.session_tree.configure(yscrollcommand=vs.set)
        self.session_tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.session_tree.bind("<<TreeviewSelect>>", self.on_session_select)

        right = tk.Frame(body, bg=T["bg_bottom"])
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self.chat_header = tk.Label(right, text="选择左侧会话查看聊天记录", bg=T["card"],
                                    fg=T["accent"],
                                    font=("Microsoft YaHei", 11, "bold"), anchor="w",
                                    padx=12, pady=8,
                                    highlightthickness=1,
                                    highlightbackground=T["card_border"])
        self.chat_header.pack(fill="x")
        self.chat_progress = SlimProgress(right, height=3, bg=T["card"],
                                          track="#EAF2FC", fill=T["accent"],
                                          light=T["accent_light"])
        T = THEME
        chat_frame = tk.Frame(right, bg=T["bg_bottom"])
        chat_frame.pack(fill="both", expand=True)
        self.chat_canvas = tk.Canvas(chat_frame, bg=T["bg_bottom"], bd=0,
                                     highlightthickness=0)
        cs = ttk.Scrollbar(chat_frame, orient="vertical", command=self.chat_canvas.yview)
        self.chat_canvas.configure(yscrollcommand=cs.set)
        self.chat_canvas.pack(side="left", fill="both", expand=True)
        cs.pack(side="right", fill="y")
        self.chat_canvas.bind("<Button-1>", self.on_chat_click)
        self.chat_canvas.bind("<MouseWheel>",
                              self._on_chat_wheel)
        self.chat_canvas.bind("<Configure>", self._on_canvas_resize)
        self.f_content = tkfont.Font(family="Microsoft YaHei", size=10)
        self.f_time = tkfont.Font(family="Microsoft YaHei", size=8)
        self.f_sender = tkfont.Font(family="Microsoft YaHei", size=9, weight="bold")
        self.f_date = tkfont.Font(family="Microsoft YaHei", size=9)
        self.f_system = tkfont.Font(family="Microsoft YaHei", size=9)
        self._resize_pending = False

        self.status_var = tk.StringVar(value="就绪")
        status = tk.Label(self.page_viewer, textvariable=self.status_var, anchor="w",
                          bg="#E8F1FC", fg=THEME["text_sub"],
                          padx=12, pady=4, font=("Microsoft YaHei", 9))
        status.pack(fill="x", side="bottom")

        self._build_loading_overlay(body)

    # ---------- 加载遮罩（非模态，替代原来的加载弹窗） ----------
    def _build_loading_overlay(self, master):
        T = THEME
        ov = tk.Frame(master, bg=T["bg_bottom"])
        card = tk.Frame(ov, bg=T["card"], padx=30, pady=24,
                        highlightthickness=1,
                        highlightbackground=T["card_border"])
        card.place(relx=0.5, rely=0.46, anchor="center")
        tk.Frame(card, bg=T["card"], width=380, height=0).pack()
        self.load_title = tk.Label(card, text="正在读取聊天数据", bg=T["card"],
                                   fg=T["text_main"], anchor="w",
                                   font=("Microsoft YaHei", 15, "bold"))
        self.load_title.pack(fill="x")
        self.load_stage = tk.Label(card, text="准备中…", bg=T["card"],
                                   fg=T["text_sub"], anchor="w",
                                   font=("Microsoft YaHei", 9))
        self.load_stage.pack(fill="x", pady=(7, 0))
        self.skeleton = tk.Canvas(card, height=84, bg=T["card"],
                                  highlightthickness=0, bd=0)
        self.skeleton.pack(fill="x", pady=(14, 0))
        self._draw_skeleton()
        row = tk.Frame(card, bg=T["card"])
        row.pack(fill="x", pady=(14, 0))
        self.load_progress = SlimProgress(row, width=330, height=10,
                                          bg=T["card"])
        self.load_progress.pack(side="left", fill="x", expand=True)
        self.load_pct = tk.Label(row, text="0%", bg=T["card"],
                                 fg=T["text_sub"], width=5, anchor="e",
                                 font=("Microsoft YaHei", 9))
        self.load_pct.pack(side="right", padx=(10, 0))
        btnrow = tk.Frame(card, bg=T["card"])
        btnrow.pack(fill="x", pady=(16, 0))
        self.btn_load_cancel = GButton(btnrow, "取消加载",
                                       command=self._cancel_loading,
                                       kind="ghost", width=100, height=32)
        self.btn_load_cancel.pack(side="right")
        self.load_overlay = ov

    def _draw_skeleton(self):
        T = THEME
        c = self.skeleton
        c.delete("all")
        try:
            width = c.winfo_width()
        except Exception:
            width = 380
        width = width if width > 100 else 380
        rows = [(12, 4, 150), (width - 172, 32, 160), (12, 60, 118)]
        for x, y, w in rows:
            ui_kit.draw_rrect(c, x, y, x + w, y + 24, 12,
                              fill=T["skeleton"], outline="")

    def _show_loading_overlay(self):
        try:
            self.load_overlay.place(relwidth=1, relheight=1)
            self.load_overlay.lift()
            self.load_progress.set(0, animate=False)
            self.load_pct.configure(text="0%")
            self.load_stage.configure(text="准备中…")
            self.btn_load_cancel.set_enabled(True)
            self.btn_load_cancel.set_text("取消加载")
            self._draw_skeleton()
        except Exception:
            pass

    def _hide_loading_overlay(self):
        try:
            self.load_overlay.place_forget()
        except Exception:
            pass

    def _cancel_loading(self):
        self._load_cancel = True
        self._cancel_event.set()
        self.btn_load_cancel.set_enabled(False)
        self.btn_load_cancel.set_text("正在取消…")
        self.load_stage.configure(text="正在停止加载…")

    def _on_chat_wheel(self, event):
        """滚轮滚动聊天区。"""
        delta = -int(event.delta / 120) * 2
        self.chat_canvas.yview_scroll(delta, "units")

    def _add_placeholder(self, entry, text: str):
        """给 ttk.Entry 加灰色占位提示（获得焦点即清空）。"""
        entry.insert(0, text)
        entry.configure(foreground=THEME["text_weak"])

        def _in(_e=None):
            if entry.get() == text:
                entry.delete(0, "end")
                entry.configure(foreground=THEME["text_main"])

        def _out(_e=None):
            if not entry.get().strip():
                entry.delete(0, "end")
                entry.insert(0, text)
                entry.configure(foreground=THEME["text_weak"])

        entry.bind("<FocusIn>", _in)
        entry.bind("<FocusOut>", _out)

    def enter_viewer(self):
        self._mark("viewer")
        if self.current_account:
            name = self.current_account.get("name", "")
            try:
                self._view_title.itemconfigure(
                    self._view_title._sub_id,
                    text=f"账号：{name}　·　微信 {self.detect_version}")
            except Exception:
                pass
        self._show_viewer()
        if self.temp_data:
            self._load_cancel = False
            self._cancel_event.clear()
            self._show_loading_overlay()
            threading.Thread(
                target=self._load_worker,
                args=(self.temp_data / "db_storage",),
                daemon=True).start()

    def _close_loading_dialog(self):
        """兼容旧事件：等价于收起加载遮罩。"""
        self._hide_loading_overlay()

    def _load_worker(self, db_storage: Path):
        """后台线程：分阶段加载，全部完成后才报告结束。"""
        _t0 = time.time()

        def prog(pct, text):
            self.event_queue.put(("load_progress", (pct, text)))

        def stage(name):
            self.event_queue.put(
                ("log", f"[加载] {name} 用时 {time.time() - _t0:.1f}s"))

        def cancelled() -> bool:
            if self._load_cancel or self._cancel_event.is_set():
                self.event_queue.put(("load_done", None))
                self.event_queue.put(("log", "已取消加载（已加载的部分仍可查看）。"))
                return True
            return False

        try:
            prog(4, "正在发现数据库…")
            stage("开始")
            svc = DBService(str(db_storage))

            def db_progress(frac):
                # 数据库初始化进度：4 → 38
                self.event_queue.put(
                    ("load_progress", (4 + 34 * frac, "正在读取聊天数据库…")))

            svc.init(progress_cb=db_progress)
            stage("数据库初始化完成")
            if cancelled():
                return
            prog(38, "正在读取会话列表…")
            sessions = svc.get_sessions(limit=0).items
            stage(f"会话列表（{len(sessions)} 个）")
            self.svc = svc
            self._db_storage = db_storage
            self.current_talker = None
            self.event_queue.put(("sessions_loaded", sessions))
            if cancelled():
                return
            prog(62, "正在统计会话消息数…")
            counts = self._counts_sync(sessions)
            stage("会话计数完成")
            self.event_queue.put(("sessions_counts", counts))
            if cancelled():
                return
            prog(84, "正在加载头像…")
            self.avatar_store.load_async(
                self._db_storage, self.event_queue,
                [s.username for s in sessions],
                on_done=None,
                progress_cb=None)
            stage("头像线程已启动")
            # 图片密钥必须准备（否则聊天里的图全是占位符）；
            # 表情是否加载由 _load_emoticons 在后台线程内部决定。
            if not self._emoji_setup_started and self.current_account:
                self._emoji_setup_started = True
                threading.Thread(target=self._emoticon_setup_worker,
                                 daemon=True).start()
            if self._load_emoticons:
                self.event_queue.put(
                    ("log", "表情与图片密钥在后台准备，不阻塞浏览。"))
            else:
                self.event_queue.put(
                    ("log", "快速启动：跳过表情，图片密钥仍在后台准备。"))
            prog(100, "加载完成")
            stage("全部完成")
            self.event_queue.put(("load_done", None))
        except Exception as exc:
            self.event_queue.put(("load_error", str(exc)))

    def _counts_sync(self, sessions) -> dict:
        """直读解密库统计每个会话的消息条数（后台线程执行）。

        每个数据库只连接一次，批量统计所有 Msg_ 表，避免反复建连。
        """
        counts: dict = {}
        # 每个库：表名 -> COUNT
        per_db: list[dict] = []
        for db in getattr(self.svc, "_message_dbs", []):
            try:
                conn = self.svc._open_db(db)  # 复用连接缓存，避免重复打开大库
                cur = conn.cursor()
                cur.execute("SELECT name FROM sqlite_master "
                            "WHERE type='table' AND name LIKE 'Msg_%'")
                tables = [r[0] for r in cur.fetchall()]
                table_counts = {}
                for t in tables:
                    try:
                        cur.execute(f'SELECT COUNT(*) FROM "{t}"')
                        table_counts[t] = cur.fetchone()[0]
                    except Exception:
                        table_counts[t] = 0
                per_db.append(table_counts)
            except Exception:
                pass
        for s in sessions:
            table = talker_to_table_name(s.username or "")
            total = 0
            for tc in per_db:
                total += tc.get(table, 0)
            counts[s.username] = total
        return counts

    def _emoticon_setup_worker(self):
        """后台并行提取表情 / 图片密钥并初始化媒体库（不阻塞 UI）。

        图片密钥优先（聊天图片是核心功能）；表情与图片并行扫描，
        单次内存扫描结果复用，总耗时由最慢的一条决定（约 11s）。
        """
        account_dir = Path(self.current_account["dir"]) if self.current_account else None
        if not account_dir:
            self._emoji_done_event.set()
            return

        def log(m):
            self.event_queue.put(("log", m))

        # 先并行扫描一次内存 seed，供表情/图片共用（微信未运行时为空）
        seeds: set[int] = set()
        pid = None
        try:
            pid = wxdecrypt._weixin_main_pid()
            if pid:
                log(f"正在扫描 Weixin.exe (PID {pid}) 内存密钥…")
                seeds = wxdecrypt.scan_emoticon_seeds_cached(pid, log=log)
                log(f"扫描到 {len(seeds)} 个候选，正在验证…")
        except Exception:
            pass

        def setup_emoticon():
            try:
                if not self._load_emoticons:
                    return
                found = wxdecrypt.find_emoticon_key(
                    account_dir, seeds=seeds, log=log)
                if not found:
                    return
                _, _, key = found
                emoji_dir = account_dir / "business" / "emoticon"
                if not emoji_dir.is_dir():
                    return
                db_urls: dict = {}
                emo_db = (self.temp_data / "db_storage" / "emoticon"
                          / "emoticon.decrypted.db")
                if emo_db.is_file():
                    try:
                        conn = sqlite3.connect(str(emo_db))
                        cur = conn.cursor()
                        cur.execute(
                            "SELECT md5, aes_key, cdn_url, encrypt_url "
                            "FROM kNonStoreEmoticonTable")
                        for md5, aes, cdn, enc in cur.fetchall():
                            if md5:
                                db_urls[md5] = (aes or "", cdn or "", enc or "")
                        conn.close()
                    except Exception:
                        pass
                cache_dir = (self.temp_data / "emoticon_cache"
                             if self.temp_data
                             else Path(tempfile.mkdtemp(prefix="wx_emoji_")))
                self.emoticon_store = EmoticonStore(
                    str(emoji_dir), key, cache_dir,
                    log=log,
                    on_ready=lambda md5: self.event_queue.put(
                        ("emoticon_ready", md5)),
                    db_urls=db_urls)
                self.event_queue.put(("emoticon_ready", None))
            except Exception:
                pass

        def setup_image():
            try:
                img_keys = wxdecrypt.find_image_keys(
                    account_dir, seeds=seeds, log=log)
                if not img_keys:
                    return
                xor_key, aes_key = img_keys
                img_cache = (self.temp_data / "image_cache"
                             if self.temp_data
                             else Path(tempfile.mkdtemp(prefix="wx_img_")))
                self.image_store = ImageStore(
                    str(account_dir), aes_key, xor_key, img_cache,
                    log=log,
                    on_ready=lambda k: self.event_queue.put(
                        ("emoticon_ready", k)))
                # 图片库就绪：强制重绘当前会话，刷新占位符
                self.event_queue.put(("image_ready", None))
            except Exception:
                pass

        threads = [threading.Thread(target=setup_emoticon, daemon=True),
                   threading.Thread(target=setup_image, daemon=True)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self._emoji_done_event.set()

    def on_sort_change(self, _event=None):
        self.sort_mode = "count" if self.sort_combo.get() == "数量" else "time"
        self.sort_order = "asc" if self.order_combo.get() == "正序" else "desc"
        self.render_sessions(self.sessions)

    def render_sessions(self, sessions):
        """按 群聊/私聊/空会话 分组渲染，支持时间或数量排序。"""
        self.session_tree.delete(*self.session_tree.get_children())
        groups = {"私聊": [], "群聊": [], "空会话": []}
        for s in sessions:
            count = self.session_counts.get(s.username, 0)
            if count == 0:
                groups["空会话"].append((s, count))
            elif "@chatroom" in (s.username or ""):
                groups["群聊"].append((s, count))
            else:
                groups["私聊"].append((s, count))
        if self.sort_mode == "count":
            key = lambda item: (item[1], item[0].time or "")
            reverse = self.sort_order == "desc"
        else:
            key = lambda item: (item[0].time or "",)
            reverse = self.sort_order == "desc"
        for gname in ("私聊", "群聊", "空会话"):
            items = sorted(groups[gname], key=key, reverse=reverse)
            if not items:
                continue
            open_state = gname != "空会话"  # 0 条的默认折叠
            node = self.session_tree.insert(
                "", "end", text=f"{gname}（{len(items)}）", open=open_state)
            seen = set()
            for s, count in items:
                if s.username in seen:
                    continue
                seen.add(s.username)
                self.avatar_store.ensure(s.username, s.nick_name or "")
                photo = self.avatar_store.get(s.username)
                self.session_tree.insert(
                    node, "end", iid=s.username,
                    text=s.nick_name or s.username,
                    image=photo,
                    values=(count if count else "", (s.time or "")[:10]))

    def filter_sessions(self):
        keyword = self.session_search.get().strip()
        if keyword == getattr(self, "_session_search_ph", None):
            keyword = ""
        keyword = keyword.lower()
        if not keyword:
            self.render_sessions(self.sessions)
            return
        filtered = [
            s for s in self.sessions
            if keyword in (s.nick_name or "").lower() or keyword in (s.username or "").lower()
        ]
        self.render_sessions(filtered)
        # 搜索时展开全部分组并滚动到第一个匹配
        for node in self.session_tree.get_children(""):
            self.session_tree.item(node, open=True)
        if filtered:
            first = filtered[0].username
            if self.session_tree.exists(first):
                self.session_tree.see(first)

    # ---------- 会话 ----------
    def on_session_select(self, _event=None):
        selection = self.session_tree.selection()
        if not selection:
            return
        talker = selection[0]
        if talker == self.current_talker:
            return
        self.current_talker = talker
        self._render_limit = 150
        self.reload_current()

    def reload_current(self):
        if not self.svc or not self.current_talker:
            return
        if self.loading:
            return
        self.loading = True
        self.status_var.set("正在读取消息…")
        self._show_work_progress("正在读取消息…", "正在加载聊天记录")
        self.after(50, self._load_messages)

    def _show_work_progress(self, status_text: str, title: str):
        """使用中操作的进度提示：聊天标题下方一条 3px 细进度条（不弹窗）。"""
        self.status_var.set(status_text)
        try:
            if not self.chat_progress.winfo_ismapped():
                self.chat_progress.pack(fill="x", after=self.chat_header)
            self.chat_progress.set(12, animate=False)
            self.chat_progress.start_crawl(12, 90, 2.2)
        except Exception:
            pass

    def _close_work_progress(self):
        try:
            self.chat_progress.stop_crawl()
            self.chat_progress.set(100, animate=False)
            self.chat_progress.pack_forget()
        except Exception:
            pass

    def _load_messages(self):
        try:
            date_from = self.date_from.get().strip()
            date_to = self.date_to.get().strip()
            keyword = self.search_var.get().strip()
            talker = self.current_talker

            def _worker():
                try:
                    msgs, total = self.svc.get_messages(
                        talker, start_time=date_from or None,
                        end_time=date_to or None, keyword=keyword or "",
                        limit=0)
                    self.event_queue.put(("msgs_loaded", (talker, msgs, total)))
                except Exception as exc:
                    self.event_queue.put(("msgs_load_error", str(exc)))

            threading.Thread(target=_worker, daemon=True).start()
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"读取消息失败：\n{exc}")
            self.status_var.set("读取失败")
            self.loading = False

    def render_messages(self, msgs):
        """渲染会话消息。

        只画最近 N 条（默认 150），顶部给出“显示更早”入口。
        几千条消息全量塞进 Canvas 会让每次重绘卡住界面，滚动也会拖影。
        """
        c = self.chat_canvas
        keep_view = getattr(self, "_keep_view", False)
        old_frac = c.yview()[0] if keep_view else None
        self.current_msgs = msgs
        self._rendered_talker = self.current_talker
        c.delete("all")
        canvas_w = max(c.winfo_width(), 400)
        wrap_limit = int(canvas_w * 0.62)
        limit = getattr(self, "_render_limit", 150)
        start = max(0, len(msgs) - limit)
        visible = msgs[start:]
        y = 10
        if start > 0:
            label = (f"↑ 显示更早的 {start} 条"
                     f"（共 {len(msgs)} 条，已显示最近 {len(visible)} 条）")
            ui_kit.draw_rrect(c, canvas_w / 2 - 200, y,
                              canvas_w / 2 + 200, y + 26, 13,
                              fill="#E7F0FC", outline="")
            c.create_text(canvas_w / 2, y + 13, text=label,
                          font=self.f_date, fill=THEME["accent"],
                          tags=("more",))
            c.tag_bind("more", "<Button-1>", self._show_earlier)
            y += 38
        last_date = ""
        for idx, m in enumerate(visible):
            tag = f"msg_{idx}"
            day = (m.time or "")[:10]
            if day and day != last_date:
                last_date = day
                pill_w = self.f_date.measure(day) + 30
                ui_kit.draw_rrect(c, canvas_w / 2 - pill_w / 2, y,
                                  canvas_w / 2 + pill_w / 2, y + 21, 10,
                                  fill="#E7F0FC", outline="")
                c.create_text(canvas_w / 2, y + 10.5, text=day,
                              font=self.f_date, fill=THEME["text_sub"])
                y += 32
            if m.type == MSG_TYPE_SYSTEM:
                for ln in wrap_lines(m.content or "", self.f_system, canvas_w - 120):
                    c.create_text(canvas_w / 2, y, text=ln, font=self.f_system,
                                  fill=THEME["text_weak"])
                    y += self.f_system.metrics("linespace")
                y += 10
                continue
            if m.type == MSG_TYPE_ANIMATION:
                y = self._render_animation(c, m, tag, y, canvas_w)
                continue
            if m.type == MSG_TYPE_IMAGE:
                y = self._render_image(c, m, tag, y, canvas_w)
                continue
            if m.type == MSG_TYPE_FILE:
                y = self._render_file(c, m, tag, y, canvas_w)
                continue
            side = "self" if m.is_self else "other"
            content = pretty_content(m)
            lines = wrap_lines(content, self.f_content, wrap_limit - 30)
            line_h = self.f_content.metrics("linespace")
            bubble_w = max(self.f_content.measure(ln) for ln in lines) + 30
            bubble_w = min(bubble_w, wrap_limit)
            bubble_h = len(lines) * line_h + 12
            time_str = m.time[11:16] if m.time and len(m.time) >= 16 else (m.time or "")
            sender_h = 0
            if side == "other" and m.is_chatroom and m.sender_name:
                sender_h = 18
            avatar_w = 40
            if side == "other":
                x1 = 10 + avatar_w + 8
                avatar_photo = self.avatar_store.get(m.sender or m.talker)
                avatar_x = 10 + avatar_w // 2
            else:
                x1 = canvas_w - 10 - avatar_w - 8 - bubble_w
                avatar_photo = self._self_avatar()
                avatar_x = canvas_w - 10 - avatar_w // 2
            by = y + sender_h
            avatar_y = by + bubble_h // 2
            if avatar_photo:
                c.create_image(avatar_x, avatar_y, image=avatar_photo,
                               anchor="center", tags=(tag,))
            if sender_h:
                c.create_text(x1 + 14, y, text=m.sender_name, font=self.f_sender,
                              fill=THEME["accent_dark"], anchor="nw", tags=(tag,))
            if side == "self":
                self._grad_bubble(c, x1, by, x1 + bubble_w, by + bubble_h, 8,
                                  THEME["bubble_self_top"],
                                  THEME["bubble_self_bottom"],
                                  tags=(tag,))
            else:
                self._grad_bubble(c, x1, by, x1 + bubble_w, by + bubble_h, 8,
                                  THEME["bubble_other"],
                                  THEME["bubble_other_bottom"],
                                  outline=THEME["bubble_border"], tags=(tag,))
            tx = x1 + 15
            ty = by + 6
            for ln in lines:
                c.create_text(tx, ty, text=ln, font=self.f_content,
                              fill=THEME["text_main"], anchor="nw", tags=(tag,))
                ty += line_h
            if side == "self":
                c.create_text(x1 + bubble_w, by + bubble_h + 2, text=time_str,
                              font=self.f_time, fill=THEME["text_weak"], anchor="ne", tags=(tag,))
            else:
                c.create_text(x1, by + bubble_h + 2, text=time_str,
                              font=self.f_time, fill=THEME["text_weak"], anchor="nw", tags=(tag,))
            y = by + bubble_h + 20
        c.configure(scrollregion=(0, 0, canvas_w, y + 8))
        if old_frac is not None:
            c.yview_moveto(old_frac)
        else:
            c.yview_moveto(1.0)

    def _show_earlier(self, _event=None):
        """向上追加更早的消息，并保持当前阅读位置不跳。"""
        c = self.chat_canvas
        try:
            old_total = float(c.bbox("all")[3])
            old_top = float(c.canvasy(0))
        except Exception:
            old_total, old_top = 0.0, 0.0
        self._render_limit = getattr(self, "_render_limit", 150) + 200
        self._keep_view = False
        self.render_messages(self.current_msgs)
        try:
            new_total = float(c.bbox("all")[3])
            if new_total > 1:
                frac = (old_top + (new_total - old_total)) / new_total
                c.yview_moveto(max(0.0, min(1.0, frac)))
        except Exception:
            pass

    def _render_image(self, c, m, tag, y, canvas_w):
        """渲染聊天图片：本地 V2 .dat 解密显示；缺失时占位并后台补。"""
        rel_path = (m.contents or {}).get("path", "")
        md5 = (m.contents or {}).get("md5", "")
        key = rel_path or md5
        photo = None
        if self.image_store and key:
            photo = self.image_store.get_photo(rel_path, md5)
        if (photo is None and self.image_store and key
                and not self.image_store.is_pending(key)):
            self.image_store.mark_pending(key)
            self.image_store.fetch_async(rel_path, md5)

        side = "self" if m.is_self else "other"
        time_str = m.time[11:16] if m.time and len(m.time) >= 16 else (m.time or "")
        sender_h = 18 if (side == "other" and m.is_chatroom and m.sender_name) else 0
        avatar_w = 40
        if photo:
            bubble_w, bubble_h = photo.width(), photo.height()
        else:
            bubble_w = self.f_content.measure("[图片]") + 20
            bubble_h = self.f_content.metrics("linespace") + 10
        if side == "other":
            x1 = 10 + avatar_w + 8
            avatar_photo = self.avatar_store.get(m.sender or m.talker)
            avatar_x = 10 + avatar_w // 2
        else:
            x1 = canvas_w - 10 - avatar_w - 8 - bubble_w
            avatar_photo = self._self_avatar()
            avatar_x = canvas_w - 10 - avatar_w // 2
        by = y + sender_h
        avatar_y = by + bubble_h // 2
        if avatar_photo:
            c.create_image(avatar_x, avatar_y, image=avatar_photo,
                           anchor="center", tags=(tag,))
        if sender_h:
            c.create_text(x1 + 14, y, text=m.sender_name, font=self.f_sender,
                          fill="#576b95", anchor="nw", tags=(tag,))
        if photo:
            c.create_image(x1, by, image=photo, anchor="nw", tags=(tag,))
        else:
            self._grad_bubble(c, x1, by, x1 + bubble_w, by + bubble_h, 8,
                              "#FFFFFF", "#F0F4FA",
                              outline=THEME["bubble_border"], tags=(tag,))
            c.create_text(x1 + bubble_w // 2, by + bubble_h // 2,
                          text="[图片]", font=self.f_content, fill="#999999",
                          anchor="center", tags=(tag,))
        if side == "self":
            c.create_text(x1 + bubble_w, by + bubble_h + 2, text=time_str,
                          font=self.f_time, fill="#b0b0b0", anchor="ne", tags=(tag,))
        else:
            c.create_text(x1, by + bubble_h + 2, text=time_str,
                          font=self.f_time, fill="#b0b0b0", anchor="nw", tags=(tag,))
        return by + bubble_h + 20

    def _render_file(self, c, m, tag, y, canvas_w):
        """渲染文件消息：微信风格文件卡片（图标 + 文件名 + 大小/扩展名）。"""
        ct = m.contents or {}
        filename = ct.get("filename") or ct.get("title") or "文件"
        ext = (ct.get("fileext") or os.path.splitext(filename)[1].lstrip(".") or "file").lower()
        size = ct.get("size") or 0

        # 文件图标颜色按类型
        icon_colors = {
            "pdf": "#e74c3c", "doc": "#2b6cb0", "docx": "#2b6cb0",
            "xls": "#2f855a", "xlsx": "#2f855a", "ppt": "#dd6b20",
            "pptx": "#dd6b20", "zip": "#6b46c1", "rar": "#6b46c1",
            "7z": "#6b46c1", "mp4": "#c53030", "mp3": "#319795",
            "txt": "#718096", "png": "#38a169", "jpg": "#38a169",
            "jpeg": "#38a169",
        }
        icon_color = icon_colors.get(ext, "#718096")

        side = "self" if m.is_self else "other"
        time_str = m.time[11:16] if m.time and len(m.time) >= 16 else (m.time or "")
        sender_h = 18 if (side == "other" and m.is_chatroom and m.sender_name) else 0
        avatar_w = 40

        # 卡片尺寸：固定高度 64，宽度随文件名
        bubble_h = 64
        name_font = self.f_content
        size_text = f"{ext.upper()}  {self._fmt_size(size)}" if size else ext.upper()
        name_w = name_font.measure(filename)
        size_w = self.f_time.measure(size_text)
        bubble_w = max(180, min(360, 92 + max(name_w, size_w) + 40))

        if side == "other":
            x1 = 10 + avatar_w + 8
            avatar_photo = self.avatar_store.get(m.sender or m.talker)
            avatar_x = 10 + avatar_w // 2
        else:
            x1 = canvas_w - 10 - avatar_w - 8 - bubble_w
            avatar_photo = self._self_avatar()
            avatar_x = canvas_w - 10 - avatar_w // 2
        by = y + sender_h
        avatar_y = by + bubble_h // 2
        if avatar_photo:
            c.create_image(avatar_x, avatar_y, image=avatar_photo,
                           anchor="center", tags=(tag,))
        if sender_h:
            c.create_text(x1 + 14, y, text=m.sender_name, font=self.f_sender,
                          fill="#576b95", anchor="nw", tags=(tag,))

        # 卡片背景
        if side == "self":
            self._grad_bubble(c, x1, by, x1 + bubble_w, by + bubble_h, 8,
                              THEME["bubble_self_top"],
                              THEME["bubble_self_bottom"], tags=(tag,))
        else:
            self._grad_bubble(c, x1, by, x1 + bubble_w, by + bubble_h, 8,
                              "#FFFFFF", "#F0F4FA",
                              outline=THEME["bubble_border"], tags=(tag,))

        # 文件图标（圆角方块 + 扩展名缩写）
        ix, iy = x1 + 14, by + 12
        self._rounded(c, ix, iy, ix + 40, iy + 40, 8, fill=icon_color,
                      outline="", tags=(tag,))
        ext_short = ext[:4].upper()
        c.create_text(ix + 20, iy + 20, text=ext_short, fill="white",
                      font=("Microsoft YaHei", 9, "bold"),
                      anchor="center", tags=(tag,))

        # 文件名 + 大小
        tx = ix + 52
        c.create_text(tx, by + 18, text=filename, font=name_font,
                      fill="#111111", anchor="nw", tags=(tag,))
        c.create_text(tx, by + 44, text=size_text, font=self.f_time,
                      fill="#888888", anchor="nw", tags=(tag,))

        # 右侧打开提示箭头
        c.create_text(x1 + bubble_w - 16, by + bubble_h // 2, text="›",
                      font=("Microsoft YaHei", 16), fill="#b0b0b0",
                      anchor="center", tags=(tag,))

        if side == "self":
            c.create_text(x1 + bubble_w, by + bubble_h + 2, text=time_str,
                          font=self.f_time, fill="#b0b0b0", anchor="ne", tags=(tag,))
        else:
            c.create_text(x1, by + bubble_h + 2, text=time_str,
                          font=self.f_time, fill="#b0b0b0", anchor="nw", tags=(tag,))
        return by + bubble_h + 20

    @staticmethod
    def _fmt_size(size: int) -> str:
        """字节数 → 人类可读大小。"""
        if size <= 0:
            return ""
        if size < 1024:
            return f"{size}B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f}KB"
        if size < 1024 * 1024 * 1024:
            return f"{size / 1024 / 1024:.1f}MB"
        return f"{size / 1024 / 1024 / 1024:.1f}GB"

    def _render_animation(self, c, m, tag, y, canvas_w):
        """渲染表情消息：本地/CDN 图片气泡；缺失时占位并后台下载。"""
        md5 = (m.contents or {}).get("md5", "")
        cdnurl = (m.contents or {}).get("cdnurl", "")
        photo = None
        if self.emoticon_store and md5:
            photo = self.emoticon_store.get_photo(md5, cdnurl,
                                                  allow_download=False)
        if (photo is None and self.emoticon_store and md5
                and not self.emoticon_store.is_pending(md5)):
            self.emoticon_store.mark_pending(md5)
            self.emoticon_store.fetch_async(md5, cdnurl)

        side = "self" if m.is_self else "other"
        time_str = m.time[11:16] if m.time and len(m.time) >= 16 else (m.time or "")
        sender_h = 18 if (side == "other" and m.is_chatroom and m.sender_name) else 0
        avatar_w = 40
        if photo:
            bubble_w, bubble_h = photo.width(), photo.height()
        else:
            bubble_w = self.f_content.measure("[表情]") + 20
            bubble_h = self.f_content.metrics("linespace") + 10
        if side == "other":
            x1 = 10 + avatar_w + 8
            avatar_photo = self.avatar_store.get(m.sender or m.talker)
            avatar_x = 10 + avatar_w // 2
        else:
            x1 = canvas_w - 10 - avatar_w - 8 - bubble_w
            avatar_photo = self._self_avatar()
            avatar_x = canvas_w - 10 - avatar_w // 2
        by = y + sender_h
        avatar_y = by + bubble_h // 2
        if avatar_photo:
            c.create_image(avatar_x, avatar_y, image=avatar_photo,
                           anchor="center", tags=(tag,))
        if sender_h:
            c.create_text(x1 + 14, y, text=m.sender_name, font=self.f_sender,
                          fill="#576b95", anchor="nw", tags=(tag,))
        if photo:
            c.create_image(x1, by, image=photo, anchor="nw", tags=(tag,))
        else:
            self._grad_bubble(c, x1, by, x1 + bubble_w, by + bubble_h, 8,
                              "#FFFFFF", "#F0F4FA",
                              outline=THEME["bubble_border"], tags=(tag,))
            c.create_text(x1 + bubble_w // 2, by + bubble_h // 2,
                          text="[表情]", font=self.f_content, fill="#999999",
                          anchor="center", tags=(tag,))
        if side == "self":
            c.create_text(x1 + bubble_w, by + bubble_h + 2, text=time_str,
                          font=self.f_time, fill="#b0b0b0", anchor="ne", tags=(tag,))
        else:
            c.create_text(x1, by + bubble_h + 2, text=time_str,
                          font=self.f_time, fill="#b0b0b0", anchor="nw", tags=(tag,))
        return by + bubble_h + 20

    def _self_avatar(self):
        name = self.current_account.get("name", "") if self.current_account else ""
        photo = self.avatar_store.get(name)
        if not photo and name:
            short = name.rsplit("_", 1)[0] if "_" in name else name
            photo = self.avatar_store.get(short)
        if not photo:
            self.avatar_store.ensure(name or "me", "我")
            photo = self.avatar_store.get(name or "me")
        return photo

    def _rounded(self, c, x1, y1, x2, y2, r, **kw):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return c.create_polygon(pts, smooth=True, **kw)

    def _grad_bubble(self, c, x1, y1, x2, y2, r, c_top, c_bottom,
                     outline="", tags=()):
        """圆角气泡：柔和投影 + 顶部玻璃高光 + 细描边。

        旧实现每 2px 画一条横线模拟渐变，一条消息十几个画布对象；
        这里压到 3 个对象（投影/气泡/高光），长会话滚动明显更顺，
        视觉上也更接近 Frutiger Aero 的"玻璃片"感觉。
        """
        if not outline:
            outline = _shade(c_bottom, -0.18)
        # 投影（1px 右下偏移，浅蓝灰）
        c.create_polygon(
            ui_kit.rrect_points(x1 + 1, y1 + 2, x2 + 1, y2 + 2, r),
            smooth=True, fill=THEME["card_shadow"], outline="", tags=tags)
        self._rounded(c, x1, y1, x2, y2, r, fill=c_bottom,
                      outline=outline, width=1, tags=tags)
        # 顶部高光：一条更亮的细线，模拟玻璃反射
        c.create_line(x1 + r + 2, y1 + 1.5, x2 - r - 2, y1 + 1.5,
                      fill=_shade(c_bottom, 0.32), width=2, tags=tags)

    def _on_canvas_resize(self, _event=None):
        if self._resize_pending:
            return
        self._resize_pending = True
        self.after(250, self._do_resize_redraw)

    def _do_resize_redraw(self):
        self._resize_pending = False
        if self.current_talker and getattr(self, "_rendered_talker", None) == self.current_talker \
                and self.current_msgs:
            self._rerender_page()

    # ---------- 搜索 / 筛选 ----------
    def search_messages(self):
        if not self.current_talker:
            messagebox.showinfo(APP_NAME, "请先选择一个会话。")
            return
        self.reload_current()

    def clear_search(self):
        self.search_var.set("")
        self.reload_current()

    def reset_filters(self):
        self.date_from.set("")
        self.date_to.set("")
        self.search_var.set("")
        self.reload_current()

    # ---------- 详情 / 导出 / 统计 ----------
    def on_chat_click(self, event):
        c = self.chat_canvas
        item = c.find_withtag("current")
        if not item:
            return
        tags = c.gettags(item[0])
        for tag in tags:
            if tag.startswith("msg_"):
                idx = int(tag[4:])
                if 0 <= idx < len(self.current_msgs):
                    m = self.current_msgs[idx]
                    if m.type == MSG_TYPE_IMAGE:
                        self.show_large_image(m)
                    elif m.type == MSG_TYPE_ANIMATION:
                        self.show_large_emoticon(m)
                    elif m.type == MSG_TYPE_FILE:
                        self.open_wechat_file(m)
                    else:
                        self.show_message_detail(m)
                return

    def open_wechat_file(self, m):
        """定位并打开微信本地存储的文件（msg\\file\\<月份>\\<文件名>）。"""
        filename = (m.contents or {}).get("filename") or ""
        if not filename:
            messagebox.showinfo(APP_NAME, "该文件消息缺少文件名信息。")
            return
        # 在 msg\file 下按文件名递归查找（文件按接收月份归档）
        base = None
        if self.current_account:
            base = Path(self.current_account["dir"]) / "msg" / "file"
        if base and base.is_dir():
            candidates = list(base.rglob(filename))
            # 优先精确匹配文件名
            if candidates:
                self._open_or_reveal(candidates[0])
                return
        # 没找到：打开 msg\file 目录让用户自己找
        if base and base.is_dir():
            try:
                os.startfile(str(base))
            except Exception:
                pass
            return
        messagebox.showinfo(APP_NAME,
                            f"本地未找到文件：{filename}\n（可能已被清理或换设备）")

    def _open_or_reveal(self, path: Path):
        """打开文件；若当前系统无法关联打开则定位到所在目录。"""
        try:
            os.startfile(str(path))
        except Exception:
            try:
                subprocess.Popen(
                    ["explorer", "/select,", str(path)],
                    creationflags=0x08000000)
            except Exception:
                try:
                    os.startfile(str(path.parent))
                except Exception:
                    messagebox.showinfo(APP_NAME, f"文件位置：\n{path}")

    def show_large_image(self, m):
        """点开聊天图片：取原始尺寸数据并弹出大图查看窗口。"""
        rel_path = (m.contents or {}).get("path", "")
        md5 = (m.contents or {}).get("md5", "")
        data = None
        if self.image_store and (rel_path or md5):
            data = self.image_store.get_bytes_hd(rel_path, md5)
        if not data:
            messagebox.showinfo(APP_NAME, "图片尚未加载完成，请稍后再点开。")
            return
        try:
            ImageViewer(self, data, title="图片大图")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"打开大图失败：\n{exc}")

    def show_large_emoticon(self, m):
        """点开表情：取原始尺寸数据并弹出大图查看窗口。"""
        md5 = (m.contents or {}).get("md5", "")
        cdnurl = (m.contents or {}).get("cdnurl", "")
        data = None
        if self.emoticon_store and md5:
            data = self.emoticon_store.get_bytes(md5, cdnurl,
                                                 allow_download=True)
        if not data:
            messagebox.showinfo(APP_NAME, "表情尚未加载完成，请稍后再点开。")
            return
        try:
            ImageViewer(self, data, title="表情大图")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"打开大图失败：\n{exc}")

    def show_message_detail(self, m):
        lines = [
            f"时间：{m.time}",
            f"类型：{TYPE_LABELS.get(m.type, m.type)}",
            f"会话：{m.talker_name or m.talker}",
            f"发送人：{'我' if m.is_self else (m.sender_name or m.sender)}",
            "",
            "内容：",
            pretty_content(m) or "(空)",
        ]
        if m.contents:
            lines.append("")
            lines.append("媒体信息：")
            for k, v in m.contents.items():
                lines.append(f"  {k}: {v}")
        top = tk.Toplevel(self)
        top.title("消息详情")
        _place_dialog(top, 520, 360, self)
        top.transient(self)
        text = tk.Text(top, wrap="word", font=("Microsoft YaHei", 10), padx=12, pady=10)
        text.pack(fill="both", expand=True)
        text.insert("1.0", "\n".join(lines))
        text.configure(state="disabled")

    def export_current(self):
        if not self.current_talker or not self.current_msgs:
            messagebox.showinfo(APP_NAME, "当前没有可导出的消息。")
            return
        name = self.svc.get_contact_display(self.current_talker)
        filename = f"聊天记录_{name or self.current_talker}.csv"
        path = filedialog.asksaveasfilename(
            title="导出当前会话", defaultextension=".csv", initialfile=filename,
            filetypes=[("CSV 文件", "*.csv"), ("文本文件", "*.txt")])
        if not path:
            return
        msgs = list(self.current_msgs)
        self._show_work_progress("正在导出…", "导出聊天记录")

        def _worker():
            try:
                if path.lower().endswith(".txt"):
                    with open(path, "w", encoding="utf-8") as f:
                        for m in msgs:
                            who = "我" if m.is_self else (m.sender_name or m.sender)
                            f.write(f"[{m.time}] {who}：{m.content}\n")
                else:
                    with open(path, "w", newline="", encoding="utf-8-sig") as f:
                        w = csv.writer(f)
                        w.writerow(["日期时间", "发送人", "是否本人", "类型", "内容"])
                        for m in msgs:
                            who = "我" if m.is_self else (m.sender_name or m.sender)
                            w.writerow([m.time, who, "是" if m.is_self else "否",
                                        TYPE_LABELS.get(m.type, str(m.type)),
                                        m.content or ""])
                self.event_queue.put(("export_done", path))
            except Exception as exc:
                self.event_queue.put(("export_error", str(exc)))

        threading.Thread(target=_worker, daemon=True).start()

    def show_stats(self):
        if not self.current_talker or not self.current_msgs:
            messagebox.showinfo(APP_NAME, "请先选择一个会话。")
            return
        # 进度条弹窗
        progress = tk.Toplevel(self)
        progress.title("统计中")
        _place_dialog(progress, 300, 90, self)
        progress.resizable(False, False)
        progress.transient(self)
        tk.Label(progress, text="正在统计当前会话…",
                 font=("Microsoft YaHei", 10)).pack(pady=(16, 6))
        bar = ttk.Progressbar(progress, mode="indeterminate", length=240)
        bar.pack(padx=20)
        bar.start(12)
        self.status_var.set("正在统计当前会话…")
        threading.Thread(target=self._stats_worker,
                         args=(progress, bar), daemon=True).start()

    def _stats_worker(self, progress, bar):
        try:
            msgs = self.current_msgs
            total = len(msgs)
            my = sum(1 for m in msgs if m.is_self)
            times = []
            for m in msgs:
                if m.time:
                    try:
                        times.append(datetime.strptime(m.time, "%Y-%m-%d %H:%M:%S"))
                    except ValueError:
                        pass
            active_days = len({t.date() for t in times})
            span_days = max(1, (max(times).date() - min(times).date()).days + 1) if times else 0
            type_counter = {}
            for m in msgs:
                label = TYPE_LABELS.get(m.type, f"类型{m.type}")
                type_counter[label] = type_counter.get(label, 0) + 1
            first = min(times).strftime("%Y-%m-%d") if times else "—"
            last = max(times).strftime("%Y-%m-%d") if times else "—"
            lines = [
                f"会话：{self.svc.get_contact_display(self.current_talker)}",
                f"消息总数：{total}",
                f"我发出：{my}（{my/total*100:.1f}%）  收到：{total-my}（{(total-my)/total*100:.1f}%）",
                f"活跃天数：{active_days}",
                f"日均消息：{total/span_days:.1f}" if span_days else "日均消息：—",
                f"时间范围：{first} ~ {last}",
                "",
                "类型分布：",
            ]
            for label, count in sorted(type_counter.items(), key=lambda x: -x[1]):
                lines.append(f"  {label}：{count}")
            self.event_queue.put(("stats_done", (progress, bar, lines)))
        except Exception as exc:
            self.event_queue.put(("stats_error", (progress, bar, str(exc))))

    def _show_stats_result(self, progress, bar, lines):
        bar.stop()
        progress.destroy()
        top = tk.Toplevel(self)
        top.title("会话统计")
        _place_dialog(top, 380, 430, self)
        top.transient(self)
        text = tk.Text(top, wrap="word", font=("Microsoft YaHei", 10), padx=16, pady=12)
        text.pack(fill="both", expand=True)
        text.insert("1.0", "\n".join(lines))
        text.configure(state="disabled")
        self.status_var.set("统计完成")

    def _show_stats_error(self, progress, bar, msg):
        bar.stop()
        progress.destroy()
        messagebox.showerror(APP_NAME, f"统计失败：\n{msg}")
        self.status_var.set("统计失败")


def json_safe_load(path: Path):
    import json
    with open(path, encoding="utf-8") as f:
        return json.load(f)


_APP_T0 = time.monotonic()


def _init_dpi():
    """让窗口在缩放显示器上不发虚（Per-Monitor DPI Aware）。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


# ---------------- 多显示器窗口定位 ----------------

def _enum_monitors():
    """枚举显示器工作区（已扣除任务栏），主显示器排第一。"""
    try:
        u = ctypes.windll.user32
    except Exception:
        return []

    class _RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class _MI(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _RECT),
                    ("rcWork", _RECT), ("dwFlags", ctypes.c_ulong)]

    found = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                              ctypes.POINTER(_RECT), ctypes.c_double)

    def _cb(hmon, _hdc, _rc, _data):
        mi = _MI()
        mi.cbSize = ctypes.sizeof(_MI)
        if u.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcWork
            found.append((r.left, r.top, r.right - r.left, r.bottom - r.top,
                          bool(mi.dwFlags & 1)))
        return 1

    try:
        u.EnumDisplayMonitors(None, None, proc(_cb), 0)
    except Exception:
        return []
    found.sort(key=lambda m: not m[4])
    return [(x, y, w, h) for x, y, w, h, _p in found]


def _monitor_at(px, py, monitors=None):
    mons = monitors if monitors is not None else _enum_monitors()
    for m in mons:
        x, y, w, h = m
        if x <= px < x + w and y <= py < y + h:
            return m
    return mons[0] if mons else (0, 0, 1920, 1080)


def _cursor_point():
    class _PT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
    pt = _PT()
    try:
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            return pt.x, pt.y
    except Exception:
        pass
    return 0, 0


def _screen_work_area(around=None):
    """挑一块工作区：环境变量 WX_WINDOW_SCREEN > around 所在屏 > 光标所在屏 > 主屏。"""
    mons = _enum_monitors()
    spec = (os.environ.get("WX_WINDOW_SCREEN") or "").strip().lower()
    if spec:
        if spec in ("secondary", "2") and len(mons) > 1:
            return mons[1]
        if spec in ("primary", "1", "0") and mons:
            return mons[0]
    if around is not None:
        try:
            if around.winfo_viewable():
                cx = around.winfo_rootx() + max(1, around.winfo_width()) // 2
                cy = around.winfo_rooty() + max(1, around.winfo_height()) // 2
                return _monitor_at(cx, cy, mons)
        except Exception:
            pass
    cx, cy = _cursor_point()
    return _monitor_at(cx, cy, mons)


_UI_STATE_DIR = (Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
                 / "WeChatChatViewer")
_UI_STATE_FILE = _UI_STATE_DIR / "window.json"
_DIALOG_STEP = [0]


def _load_window_box():
    """读取上次的窗口位置；中心点不在任何显示器内则丢弃（拔掉副屏后不会跑到屏幕外）。"""
    try:
        with open(_UI_STATE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        x, y, w, h = int(d["x"]), int(d["y"]), int(d["w"]), int(d["h"])
    except Exception:
        return None
    mons = _enum_monitors()
    if not mons:
        return None
    cx, cy = x + w // 2, y + h // 2
    for mx, my, mw, mh in mons:
        if mx <= cx < mx + mw and my <= cy < my + mh:
            return x, y, w, h
    return None


def _save_window_box(win):
    # 注意：ChatViewer 自己有个字符串属性 self.state，会遮蔽 Tk.state()，
    # 这里必须用 wm_state()。
    try:
        if win.wm_state() != "normal":
            return
        _UI_STATE_DIR.mkdir(parents=True, exist_ok=True)
        data = {"x": win.winfo_x(), "y": win.winfo_y(),
                "w": win.winfo_width(), "h": win.winfo_height()}
        tmp = _UI_STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, _UI_STATE_FILE)
    except Exception:
        pass


def _place_main_window(win, width, height):
    """主窗口：优先恢复上次位置，否则居中到光标所在屏（不再一律怼主屏）。"""
    saved = _load_window_box()
    if saved:
        x, y, w, h = saved
        win.geometry(f"{max(900, w)}x{max(560, h)}+{x}+{y}")
        return
    ax, ay, aw, ah = _screen_work_area(None)
    px = ax + max(0, (aw - width) // 2)
    py = ay + max(0, (ah - height) // 2)
    win.geometry(f"{width}x{height}+{px}+{py}")


def _place_dialog(win, width, height, parent=None):
    """弹窗跟着父窗口所在的那块屏，层叠错开。"""
    owner = parent if parent is not None else getattr(win, "master", None)
    ax, ay, aw, ah = _screen_work_area(owner)
    step = _DIALOG_STEP[0]
    _DIALOG_STEP[0] = (step + 1) % 5
    px = ax + max(0, (aw - width) // 2) + step * 26
    py = ay + max(0, (ah - height) // 2) + step * 22
    win.geometry(f"{width}x{height}+{px}+{py}")


def main():
    # 独立进程内的内存 seed 扫描入口：--seedscan <pid> <out.json>
    if "--seedscan" in sys.argv:
        try:
            idx = sys.argv.index("--seedscan")
            sys.exit(wxdecrypt.run_seed_scan_cli(int(sys.argv[idx + 1]),
                                                 sys.argv[idx + 2]))
        except Exception:
            sys.exit(1)
    # 独立清理进程入口：--cleanup <dir>
    if "--cleanup" in sys.argv:
        try:
            target = Path(sys.argv[sys.argv.index("--cleanup") + 1])
            time.sleep(1.2)
            shutil.rmtree(target, ignore_errors=True)
        except Exception:
            pass
        return
    # 提权子进程入口：--decrypt <account_dir> <target> <log> <done>
    if "--decrypt" in sys.argv:
        try:
            idx = sys.argv.index("--decrypt")
            account_dir, target, log_file, done_file = sys.argv[idx + 1:idx + 5]
        except (IndexError, ValueError):
            account_dir = ""
            target = tempfile.mkdtemp(prefix="wx_viewer_")
            log_file, done_file = (str(p) for p in wxdecrypt.log_paths())
        sys.exit(wxdecrypt.run_headless_decrypt(account_dir, target,
                                                log_file, done_file))
    _init_dpi()
    app = ChatViewer(selftest="--selftest" in sys.argv)
    app.mainloop()


if __name__ == "__main__":
    main()
