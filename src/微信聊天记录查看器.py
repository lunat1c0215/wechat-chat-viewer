#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微信聊天记录查看器（微信 4.x 本地数据，跨电脑可用版）。

启动即自动检测本机微信与数据，未解密时弹一次管理员授权自动完成
密钥提取 + 数据库解密，随后进入气泡式聊天查看器。
"""

from __future__ import annotations

import csv
import hashlib
import io
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


# ================= Frutiger Aero + WinXP 主题 =================
# XP Luna 蓝渐变标题栏 + Frutiger 水润玻璃质感
THEME = {
    "title_top": "#3B86FF",      # 标题栏渐变顶（亮蓝）
    "title_bottom": "#1B4FCC",   # 标题栏渐变底（深蓝）
    "title_text": "#FFFFFF",
    "title_sub": "#D6E4FF",
    "bg_top": "#EAF3FF",         # 主背景渐变顶（水润浅蓝）
    "bg_bottom": "#F7FBFF",
    "card": "#FFFFFF",
    "card_border": "#B8CCE8",
    "accent": "#2B6BE4",         # XP 蓝（选中/强调）
    "accent_light": "#4A8AFF",
    "frutiger_green": "#0FBFC5", # Frutiger 翠绿点缀
    "bubble_self_top": "#8FF3B0",  # 自己气泡渐变顶（水润绿）
    "bubble_self_bottom": "#4FD67F",
    "bubble_other": "#FFFFFF",
    "bubble_border": "#C9D8EC",
    "text_main": "#1A1A1A",
    "text_sub": "#6A7A8F",
    "text_white": "#FFFFFF",
    "xphighlight": "#2B6BE4",    # 会话选中 XP 蓝
    "btn_top": "#F0F6FF",        # XP 按钮渐变顶
    "btn_bottom": "#C9DBF5",
    "btn_border_hi": "#FFFFFF",
    "btn_border_lo": "#7B9AC9",
    "tree_bg": "#FFFFFF",
    "tree_sel": "#2B6BE4",
}


def make_vgrad(w: int, h: int, c1: str, c2: str) -> Image.Image:
    """生成垂直渐变图片（Pillow），供 Tk 背景使用。"""
    img = Image.new("RGB", (max(1, w), max(1, h)))
    r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
    r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
    px = img.load()
    for y in range(img.height):
        t = y / max(1, img.height - 1)
        r = int(r1 + (r2 - r1) * t)
        g = int(g1 + (g2 - g1) * t)
        b = int(b1 + (b2 - b1) * t)
        for x in range(img.width):
            px[x, y] = (r, g, b)
    return img


def vgrad_photo(w: int, h: int, c1: str, c2: str) -> tk.PhotoImage:
    """渐变 → tk.PhotoImage（缓存避免重复生成）。"""
    img = make_vgrad(w, h, c1, c2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return tk.PhotoImage(data=buf.getvalue())


_GRAD_CACHE: dict = {}


def get_grad(w: int, h: int, c1: str, c2: str) -> tk.PhotoImage:
    """带缓存的渐变获取。"""
    key = (w, h, c1, c2)
    if key not in _GRAD_CACHE:
        _GRAD_CACHE[key] = vgrad_photo(w, h, c1, c2)
    return _GRAD_CACHE[key]

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
            img = img.resize((self.size, self.size), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        except Exception:
            return None

    def make_default(self, username: str, name: str = "") -> bytes:
        """生成彩色圆形 + 首字默认头像（PNG bytes）。"""
        s = self.size
        palette = ["#4A90D9", "#5CB85C", "#F0AD4E", "#D9534F", "#8E6BB8",
                   "#3FB6B2", "#E8838A", "#7A8BA6"]
        h = int(hashlib.md5((username or "x").encode()).hexdigest(), 16)
        color = palette[h % len(palette)]
        img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        from PIL import ImageDraw
        draw = ImageDraw.Draw(img)
        draw.ellipse((0, 0, s - 1, s - 1), fill=color)
        ch = (name or "?").strip()[:1]
        from PIL import ImageFont
        try:
            font = ImageFont.truetype("msyh.ttc", int(s * 0.55))
        except Exception:
            font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), ch, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text(((s - tw) / 2 - bbox[0], (s - th) / 2 - bbox[1]), ch,
                  font=font, fill="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def apply(self, mapping: dict):
        """主线程：把 PNG bytes 转成 PhotoImage。"""
        for username, png in mapping.items():
            try:
                self.images[username] = tk.PhotoImage(data=png)
            except Exception:
                pass

    def ensure(self, username: str, name: str = ""):
        if username not in self.images:
            try:
                self.images[username] = tk.PhotoImage(
                    data=self.make_default(username, name))
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
        self.geometry("860x640")
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
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1180x720")
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
        self.event_queue: queue.Queue = queue.Queue()
        self._heartbeat = 0  # 主线程心跳计数（看门狗监控用）
        self._start_watchdog()
        self.avatar_store = AvatarStore(40)
        self.emoticon_store: EmoticonStore | None = None
        self.image_store: ImageStore | None = None
        self._emoji_setup_started = False
        self._emoji_done_event = threading.Event()
        self._emoji_crawl_active = False
        self._rerender_job = None
        self._load_target = 0
        self._load_emoticons = True
        self._no_auto_prompt = False

        self._configure_style()
        self.page_wizard = tk.Frame(self, bg=THEME["bg_bottom"])
        self.page_viewer = tk.Frame(self, bg=THEME["bg_bottom"])
        self._build_wizard()
        self._build_viewer()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._show_wizard()
        self.after(100, self._poll_events)
        self.after(150, self._auto_start)

    def _start_watchdog(self):
        """主线程看门狗：后台线程监控主线程心跳。

        若主线程超过 2 秒未处理事件（界面冻结），把主线程当时的
        Python 调用堆栈写入临时日志，用于定位 UI 卡死根因。
        """
        def _watch():
            last_hb = 0
            last_log_time = 0
            while True:
                time.sleep(1)
                try:
                    hb = self._heartbeat
                    if hb == last_hb:
                        # 主线程 1 秒内没心跳，疑似冻结
                        stuck_for = 1
                        time.sleep(1)
                        if self._heartbeat == hb:
                            # 连续 2 秒无心跳 → 记录主线程堆栈
                            import sys as _sys
                            import traceback
                            main_id = next(
                                (t.ident for t in threading.enumerate()
                                 if t.name == "MainThread"), None)
                            stack = ""
                            if main_id:
                                frame = _sys._current_frames().get(main_id)
                                if frame:
                                    stack = "".join(
                                        traceback.format_stack(frame))
                                    # 跳过已知的正常等待点（模态弹窗等用户）
                                    if "_ask_view" in stack and \
                                            "wait_window" in stack:
                                        last_hb = hb
                                        continue
                            now = time.time()
                            if now - last_log_time > 10:  # 每 10 秒最多记一次
                                last_log_time = now
                                try:
                                    from pathlib import Path
                                    logp = Path(tempfile.gettempdir()) / "wx_viewer_hang.log"
                                    logp.write_text(
                                        f"[{time.strftime('%H:%M:%S')}] "
                                        f"主线程疑似卡住 {stuck_for}s\n{stack}\n",
                                        encoding="utf-8")
                                except Exception:
                                    pass
                    last_hb = hb
                except Exception:
                    pass

        threading.Thread(target=_watch, daemon=True).start()

    def _on_close(self):
        """退出时清理临时解密数据（无缓存）。"""
        if self.svc is not None:
            try:
                self.svc.close()
            except Exception:
                pass
        if self.temp_data and self.temp_data.is_dir():
            try:
                shutil.rmtree(self.temp_data, ignore_errors=True)
            except Exception:
                pass
        self.destroy()

    def _poll_events(self):
        self._heartbeat += 1  # 主线程活跃心跳（看门狗）
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
            account, version = payload
            self._on_wechat_detected(account, version)
        elif kind == "load_progress":
            pct, text = payload
            self._load_target = pct
            if hasattr(self, "loading_status"):
                self.loading_status.configure(text=text)
        elif kind == "sessions_loaded":
            self.sessions = payload or []
            self.session_counts = {}
            self.render_sessions(self.sessions)
        elif kind == "load_done":
            self._close_loading_dialog()
            if self.current_account:
                name = self.current_account.get("name", "")
                self.status_var.set(
                    f"已加载：{name}，{len(self.sessions)} 个会话"
                    "（临时数据，退出后清除）")
        elif kind == "load_error":
            self._close_loading_dialog()
            messagebox.showerror(APP_NAME, f"数据库加载失败：\n{payload}")
            self.status_var.set("加载失败")
        elif kind == "sessions_counts":
            self.session_counts = payload or {}
            self.render_sessions(self.sessions)
        elif kind == "avatars":
            self.avatar_store.apply(payload or {})
            for s in self.sessions:
                self.avatar_store.ensure(s.username, s.nick_name or "")
            self.render_sessions(self.sessions)
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
            if talker == self.current_talker:
                self.render_messages(msgs)
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

    def _flush_rerender(self):
        self._rerender_job = None
        if self.current_msgs:
            self._rerender_page()

    def _rerender_page(self):
        """图片/表情就绪、窗口缩放时重绘当前会话。"""
        if self.current_msgs:
            self.render_messages(self.current_msgs)

    # ---------- 样式 ----------
    def _configure_style(self):
        style = ttk.Style(self)
        T = THEME
        try:
            style.theme_use("clam")  # clam 便于自定义 XP 风
        except tk.TclError:
            pass
        # 主窗口背景
        self.configure(bg=T["bg_bottom"])
        # 会话树：XP 选中蓝
        style.configure("Treeview",
                        rowheight=46,
                        font=("Microsoft YaHei", 10),
                        background=T["tree_bg"],
                        fieldbackground=T["tree_bg"],
                        foreground=T["text_main"])
        style.map("Treeview",
                  background=[("selected", T["tree_sel"])],
                  foreground=[("selected", "#FFFFFF")])
        style.configure("Treeview.Heading",
                        font=("Microsoft YaHei", 10, "bold"),
                        background=T["btn_top"],
                        foreground=T["text_main"],
                        relief="raised")
        style.map("Treeview.Heading", background=[("active", T["btn_bottom"])])
        # XP 3D 凸起按钮
        style.configure("TButton",
                        font=("Microsoft YaHei", 10),
                        background=T["btn_top"],
                        foreground=T["text_main"],
                        bordercolor=T["btn_border_lo"],
                        lightcolor=T["btn_border_hi"],
                        darkcolor=T["btn_border_lo"],
                        relief="raised",
                        padding=(10, 4))
        style.map("TButton",
                  background=[("active", T["btn_bottom"]),
                              ("pressed", T["btn_bottom"])])
        style.configure("Tool.TButton", font=("Microsoft YaHei", 10))
        # 输入框 / 下拉框
        style.configure("TEntry", fieldbackground="#FFFFFF",
                        bordercolor=T["btn_border_lo"],
                        lightcolor=T["btn_border_hi"],
                        darkcolor=T["btn_border_lo"],
                        relief="sunken")
        style.configure("TCombobox", fieldbackground="#FFFFFF",
                        background=T["btn_top"],
                        bordercolor=T["btn_border_lo"])
        # XP 样式滚动条
        style.configure("Vertical.TScrollbar",
                        background=T["btn_top"],
                        troughcolor=T["bg_top"],
                        bordercolor=T["btn_border_lo"],
                        arrowcolor=T["accent"])
        style.configure("Horizontal.TScrollbar",
                        background=T["btn_top"],
                        troughcolor=T["bg_top"],
                        bordercolor=T["btn_border_lo"],
                        arrowcolor=T["accent"])
        # 进度条：XP 蓝色块状
        style.configure("Horizontal.TProgressbar",
                        background=T["accent"],
                        troughcolor=T["bg_top"],
                        bordercolor=T["btn_border_lo"],
                        lightcolor=T["accent_light"],
                        darkcolor=T["accent"])

    # ================= 向导页（等待微信登录 → 自动侦测 → 确认） =================
    def _build_wizard(self):
        T = THEME
        top = tk.Frame(self.page_wizard, padx=14, pady=10)
        top.bg_photo = get_grad(1200, 60, T["title_top"], T["title_bottom"])
        top_label = tk.Label(top, image=top.bg_photo, borderwidth=0)
        top_label.place(x=0, y=0, relwidth=1, relheight=1)
        top.pack(fill="x")
        tk.Label(top, text=APP_NAME, bg=T["title_top"], fg=T["title_text"],
                 font=("Microsoft YaHei", 15, "bold")).pack(side="left")
        tk.Label(top, text="等待微信登录 · 自动侦测 · 无缓存查看", bg=T["title_top"],
                 fg=T["title_sub"], font=("Microsoft YaHei", 10)).pack(side="left", padx=(16, 0))

        body = tk.Frame(self.page_wizard, bg=T["bg_bottom"])
        body.pack(fill="both", expand=True, padx=20, pady=16)

        self.wizard_text = tk.Text(body, wrap="word", font=("Microsoft YaHei", 10),
                                  bg="#ffffff", bd=1, relief="solid",
                                  highlightthickness=1,
                                  highlightbackground=T["card_border"],
                                  padx=12, pady=10,
                                  state="disabled", height=14)
        self.wizard_text.pack(fill="both", expand=True)
        self.wizard_text.tag_configure("ok", foreground="#07a64f")
        self.wizard_text.tag_configure("warn", foreground="#e6a23c")
        self.wizard_text.tag_configure("err", foreground="#d9534f")
        self.wizard_text.tag_configure("head", foreground="#333333",
                                       font=("Microsoft YaHei", 10, "bold"))

        btns = tk.Frame(body, bg=T["bg_bottom"])
        btns.pack(fill="x", pady=(12, 0))
        self.detect_button = ttk.Button(btns, text="立即检测", command=self._auto_start)
        self.detect_button.pack(side="left")
        ttk.Button(btns, text="选择数据目录", command=self.pick_root).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="退出", command=self._on_close).pack(side="right")
        self.wizard_status = tk.Label(btns, text="", bg=T["bg_bottom"], fg=T["text_sub"],
                                      font=("Microsoft YaHei", 9))
        self.wizard_status.pack(side="right", padx=(0, 12))

    def _wizard_log(self, text, tag=None):
        self.wizard_text.configure(state="normal")
        self.wizard_text.insert("end", text + "\n", tag or ())
        self.wizard_text.see("end")
        self.wizard_text.configure(state="disabled")

    def _show_wizard(self):
        self.page_viewer.pack_forget()
        self.page_wizard.pack(fill="both", expand=True)

    def _show_viewer(self):
        self.page_wizard.pack_forget()
        self.page_viewer.pack(fill="both", expand=True)

    def _auto_start(self):
        if self.waiting or self.decrypting:
            return
        self._no_auto_prompt = False
        self.wizard_text.configure(state="normal")
        self.wizard_text.delete("1.0", "end")
        self.wizard_text.configure(state="disabled")
        self.wizard_status.configure(text="等待微信登录…")
        self._wizard_log("欢迎使用微信聊天记录查看器", "head")
        self._wizard_log("")
        self._wizard_log("请先运行并登录电脑版微信。", "warn")
        self._wizard_log("稍等几秒，程序将自动侦测到该微信号。", "")
        self._begin_waiting()

    def _begin_waiting(self):
        self.waiting = True
        threading.Thread(target=self._wait_worker, daemon=True).start()

    def _wait_worker(self):
        """轮询：微信运行中且有账号数据 → 通知主线程弹窗确认。"""
        count = 0
        while self.waiting:
            proc = wxdecrypt.find_weixin_process()
            if proc:
                accounts = wxdecrypt.list_accounts(self.current_root)
                if accounts:
                    account = accounts[0]  # 最近写入的账号 = 当前登录
                    version = wxdecrypt.wechat_version(proc)
                    self.event_queue.put(("wechat_detected", (account, version)))
                    return
                self.event_queue.put(("log", "已检测到微信进程，正在等待登录数据…"))
            else:
                if count % 5 == 0:
                    self.event_queue.put(("log", "尚未检测到微信，请先运行并登录电脑版微信…"))
            count += 1
            time.sleep(2)

    def _on_wechat_detected(self, account, version):
        self.waiting = False
        self.wizard_status.configure(text="已侦测到微信号")
        self._wizard_log("", "")
        self._wizard_log(f"侦测到微信号：{account['name']}", "ok")
        if version:
            self._wizard_log(f"微信版本：{version}", "")
        self._wizard_log(f"数据大小：约 {account['size_mb']}MB", "")
        yes, load_emoji = self._ask_view(account, version)
        if yes:
            self._load_emoticons = load_emoji
            self.start_decrypt(account)
        else:
            # 点“否”后不再自动弹出询问，除非手动点“立即检测”
            self._no_auto_prompt = True
            self._wizard_log("已选择“否”，本次不再自动询问。", "warn")
            self._wizard_log("如需查看，请点击左上角“立即检测”。", "")
            self.wizard_status.configure(text="已跳过（点“立即检测”重新询问）")

    def _ask_view(self, account, version):
        """自定义确认弹窗：是否查看聊天记录 + 是否加载表情图片。"""
        dlg = tk.Toplevel(self)
        dlg.title("确认查看")
        dlg.geometry("470x250")
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.attributes("-topmost", True)
        dlg.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - 470) // 2
        y = self.winfo_rooty() + (self.winfo_height() - 250) // 2
        dlg.geometry(f"+{max(x, 0)}+{max(y, 0)}")

        tk.Label(
            dlg, justify="left", font=("Microsoft YaHei", 10),
            text=(f"侦测到微信号：{account['name']}\n"
                  f"微信版本：{version or '未知'}\n"
                  f"数据大小：约 {account['size_mb']}MB\n\n"
                  "是否查看该微信号的聊天记录？")
        ).pack(pady=(16, 2), padx=22, anchor="w")

        self._emoji_opt_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            dlg, text="加载表情图片（完整启动，表情显示更全）",
            variable=self._emoji_opt_var,
            font=("Microsoft YaHei", 9), anchor="w"
        ).pack(pady=(2, 0), padx=24, anchor="w")
        tk.Label(
            dlg, text="不勾选 = 快速启动，聊天中表情显示占位符",
            font=("Microsoft YaHei", 8), fg="#888888", anchor="w"
        ).pack(padx=42, anchor="w")

        result = {"yes": False, "emoji": True}

        def on_yes():
            result["yes"] = True
            result["emoji"] = bool(self._emoji_opt_var.get())
            dlg.destroy()

        def on_no():
            result["yes"] = False
            dlg.destroy()

        btns = tk.Frame(dlg)
        btns.pack(pady=(14, 0))
        tk.Button(btns, text="是", width=8, command=on_yes,
                  font=("Microsoft YaHei", 10)).pack(side="left", padx=10)
        tk.Button(btns, text="否", width=8, command=on_no,
                  font=("Microsoft YaHei", 10)).pack(side="left", padx=10)
        dlg.protocol("WM_DELETE_WINDOW", on_no)
        dlg.grab_set()
        self.wait_window(dlg)
        return result["yes"], result["emoji"]

    def pick_root(self):
        initial = str(self.current_root)
        selected = filedialog.askdirectory(title="选择 xwechat_files 数据根目录", initialdir=initial)
        if selected:
            self.current_root = Path(selected)
            self.waiting = False
            self._auto_start()

    def start_decrypt(self, account):
        if self.decrypting:
            return
        self.decrypting = True
        self.detect_button.configure(state="disabled")
        self.wizard_status.configure(text="正在解密…")
        self._wizard_log("", "")
        self._wizard_log("开始提取密钥并解密（本次启动临时解密，退出后自动清除）…", "head")
        # 无缓存：每次启动都新建临时目录
        if self.temp_data and self.temp_data.is_dir():
            shutil.rmtree(self.temp_data, ignore_errors=True)
        self.temp_data = Path(tempfile.mkdtemp(prefix="wx_viewer_"))
        self.current_account = account
        threading.Thread(target=self._decrypt_worker, args=(account,), daemon=True).start()

    def _decrypt_worker(self, account):
        target = self.temp_data / "db_storage"

        def log_cb(msg):
            self.event_queue.put(("log", msg))
        try:
            code = wxdecrypt.run_headless_decrypt(
                str(account["dir"]), str(target), log_cb=log_cb)
        except PermissionError:
            self.event_queue.put(("log", "普通权限读取被拒绝，改用管理员方式重试…"))
            self._elevate_fallback(target)
            return
        if code == 0:
            self.event_queue.put(("decrypt_done", None))
        else:
            self.event_queue.put(("decrypt_failed", "解密未完全成功，请重试。"))

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
            self.event_queue.put(("decrypt_failed", "无法启动管理员授权，请手动以管理员身份运行。"))
            return
        deadline = time.monotonic() + 180
        last_len = 0
        while time.monotonic() < deadline:
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
                    self.event_queue.put(("decrypt_done" if result.get("success")
                                          else "decrypt_failed",
                                          result.get("message") or "解密失败"))
                    return
        self.event_queue.put(("decrypt_failed", "解密超时，请重试。"))

    def _after_decrypt(self, success):
        self.decrypting = False
        self.detect_button.configure(state="normal")
        if success and self.temp_data:
            ready = list((self.temp_data / "db_storage").rglob("*.decrypted.db"))
            if ready:
                self._wizard_log("", "")
                self._wizard_log(f"解密完成（{len(ready)} 个数据库），正在进入查看器…", "ok")
                self.after(400, self.enter_viewer)
                return
        self._decrypt_failed("未能解密出可用数据，请确认微信已登录后重试。")

    def _decrypt_failed(self, msg):
        self.decrypting = False
        self.detect_button.configure(state="normal")
        self.wizard_status.configure(text="解密失败")
        self._wizard_log("", "")
        self._wizard_log(f"失败：{msg}", "err")
        self._wizard_log("将返回等待状态…", "")
        self._begin_waiting()

    # ================= 查看器页 =================
    def _build_viewer(self):
        T = THEME
        top = tk.Frame(self.page_viewer, padx=14, pady=10)
        top.bg_photo = get_grad(1200, 60, T["title_top"], T["title_bottom"])
        top_label = tk.Label(top, image=top.bg_photo, borderwidth=0)
        top_label.place(x=0, y=0, relwidth=1, relheight=1)
        top.pack(fill="x")
        tk.Label(top, text=APP_NAME, bg=T["title_top"], fg=T["title_text"],
                 font=("Microsoft YaHei", 15, "bold")).pack(side="left")
        tk.Label(top, text="账号：", bg=T["title_top"], fg=T["title_sub"],
                 font=("Microsoft YaHei", 10)).pack(side="left", padx=(16, 2))
        self.account_combo = ttk.Combobox(top, state="readonly", width=26,
                                          font=("Microsoft YaHei", 9))
        self.account_combo.pack(side="left")
        self.account_combo.bind("<<ComboboxSelected>>", self.on_account_switch)
        ttk.Button(top, text="选择数据目录", command=self.pick_root).pack(side="right")

        bar = tk.Frame(self.page_viewer, bg=T["card"], padx=12, pady=8,
                       highlightthickness=1,
                       highlightbackground=T["card_border"])
        bar.pack(fill="x")
        tk.Label(bar, text="搜索:", bg=T["card"], font=("Microsoft YaHei", 10)).pack(side="left")
        self.search_var = tk.StringVar()
        search_entry = ttk.Entry(bar, textvariable=self.search_var, width=22)
        search_entry.pack(side="left", padx=(2, 6))
        search_entry.bind("<Return>", lambda e: self.search_messages())
        ttk.Button(bar, text="搜索", command=self.search_messages).pack(side="left")
        ttk.Button(bar, text="清除", command=self.clear_search).pack(side="left", padx=(4, 0))
        tk.Label(bar, text="  时间:", bg=T["card"], font=("Microsoft YaHei", 10)).pack(side="left")
        self.date_from = ttk.Entry(bar, width=11)
        self.date_from.pack(side="left", padx=(2, 2))
        tk.Label(bar, text="至", bg=T["card"]).pack(side="left")
        self.date_to = ttk.Entry(bar, width=11)
        self.date_to.pack(side="left", padx=(2, 2))
        ttk.Button(bar, text="筛选", command=self.reload_current).pack(side="left", padx=(4, 0))
        ttk.Button(bar, text="重置", command=self.reset_filters).pack(side="left", padx=(4, 0))
        ttk.Button(bar, text="导出当前会话", command=self.export_current).pack(side="right")
        ttk.Button(bar, text="统计", command=self.show_stats).pack(side="right", padx=(0, 6))

        body = tk.Frame(self.page_viewer, bg=T["bg_top"])
        body.pack(fill="both", expand=True, padx=10, pady=(10, 6))

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
                          bg=THEME["btn_bottom"], fg=THEME["text_main"],
                          padx=12, pady=4, font=("Microsoft YaHei", 9))
        status.pack(fill="x", side="bottom")

    def _on_chat_wheel(self, event):
        """滚轮滚动聊天区。"""
        delta = -int(event.delta / 120) * 2
        self.chat_canvas.yview_scroll(delta, "units")

    # ---------- 账号 ----------
    def on_account_switch(self, _event=None):
        # 无缓存模式：仅当前已解密的账号可查看
        self.status_var.set("当前仅支持查看本次侦测到的账号，切换账号请重启本软件。")

    def enter_viewer(self):
        if self.current_account:
            self.account_combo["values"] = [self.current_account["name"]]
            self.account_combo.set(self.current_account["name"])
        self._show_viewer()
        if self.temp_data:
            self._show_loading_dialog()
            threading.Thread(
                target=self._load_worker,
                args=(self.temp_data / "db_storage",),
                daemon=True).start()

    def _show_loading_dialog(self):
        """进入查看器时的加载进度弹窗（主线程创建）。"""
        T = THEME
        dlg = tk.Toplevel(self)
        dlg.title("正在加载")
        dlg.geometry("430x150")
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.attributes("-topmost", True)
        dlg.protocol("WM_DELETE_WINDOW", lambda: None)
        dlg.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - 430) // 2
        y = self.winfo_rooty() + (self.winfo_height() - 150) // 2
        dlg.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        head = tk.Frame(dlg)
        head.bg_photo = get_grad(430, 34, T["title_top"], T["title_bottom"])
        hl = tk.Label(head, image=head.bg_photo, borderwidth=0)
        hl.place(x=0, y=0, relwidth=1, relheight=1)
        head.pack(fill="x")
        tk.Label(head, text="⏳ 正在加载聊天数据…", bg=T["title_top"],
                 fg=T["title_text"], font=("Microsoft YaHei", 12, "bold")
                 ).pack(pady=(6, 4))
        dlg.configure(bg=T["bg_bottom"])
        self.loading_status = tk.Label(dlg, text="准备中…",
                                       font=("Microsoft YaHei", 9),
                                       fg=T["text_sub"],
                                       bg=T["bg_bottom"])
        self.loading_status.pack(pady=(14, 4))
        # indeterminate 模式：加载期间进度条持续滚动动画，
        # 不存在"卡住"的视觉；完成后关闭弹窗。
        self.loading_bar = ttk.Progressbar(dlg, mode="indeterminate",
                                           length=350)
        self.loading_bar.pack(padx=30)
        self.loading_dialog = dlg
        self.loading_bar.start(10)

    def _close_loading_dialog(self):
        dlg = getattr(self, "loading_dialog", None)
        if dlg is not None and dlg.winfo_exists():
            try:
                if getattr(self, "loading_bar", None) is not None:
                    self.loading_bar.stop()
                dlg.destroy()
            except Exception:
                pass
        self.loading_dialog = None
        self.loading_bar = None

    def _load_worker(self, db_storage: Path):
        """后台线程：分阶段加载，全部完成后才报告结束。"""
        _t0 = time.time()
        _hang_log = Path(tempfile.gettempdir()) / "wx_viewer_load.log"
        try:
            _hang_log.write_text("", encoding="utf-8")
        except Exception:
            pass

        def prog(pct, text):
            self.event_queue.put(("load_progress", (pct, text)))

        def stage(name):
            self.event_queue.put(
                ("log", f"[加载] {name} 用时 {time.time() - _t0:.1f}s"))
            try:
                with open(_hang_log, "a", encoding="utf-8") as f:
                    f.write(f"{time.time() - _t0:6.1f}s  {name}\n")
            except Exception:
                pass

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
            prog(38, "正在读取会话列表…")
            sessions = svc.get_sessions(limit=0).items
            stage(f"会话列表（{len(sessions)} 个）")
            self.svc = svc
            self._db_storage = db_storage
            self.current_talker = None
            self.event_queue.put(("sessions_loaded", sessions))
            prog(62, "正在统计会话消息数…")
            counts = self._counts_sync(sessions)
            stage("会话计数完成")
            self.event_queue.put(("sessions_counts", counts))
            prog(84, "正在加载头像…")
            avatar_done = threading.Event()
            avatar_prog = [84.0]

            def avatar_progress(frac):
                # 头像阶段：84 → 88
                avatar_prog[0] = 84.0 + frac
                self.event_queue.put(("load_progress",
                                      (avatar_prog[0], "正在加载头像…")))

            self.avatar_store.load_async(
                self._db_storage, self.event_queue,
                [s.username for s in sessions],
                on_done=avatar_done.set,
                progress_cb=avatar_progress)
            stage("头像线程已启动")
            if self._load_emoticons:
                self._emoji_done_event.clear()
                if not self._emoji_setup_started and self.current_account:
                    self._emoji_setup_started = True
                    threading.Thread(target=self._emoticon_setup_worker,
                                     daemon=True).start()
            # 等待头像完成（头像阶段进度 84→88 由 progress_cb 上报）
            avatar_done.wait(45)
            stage("头像加载完成")
            if self._load_emoticons:
                prog(92, "正在准备表情支持…")
                # 表情阶段等待期间进度条持续缓慢推进 92→99（避免看似卡住）
                self._emoji_crawl_active = True

                def emoji_crawl():
                    p = 92.0
                    direction = 1
                    while self._emoji_crawl_active and \
                            not self._emoji_done_event.is_set():
                        if p < 99.2:
                            p = min(99.2, p + 0.35)
                        else:
                            # 到顶后在 98.6~99.2 之间轻呼吸，保持"活着"的观感
                            p = p + 0.06 * direction
                            if p > 99.2:
                                p, direction = 99.2, -1
                            elif p < 98.6:
                                p, direction = 98.6, 1
                        self.event_queue.put(
                            ("load_progress", (p, "正在准备表情支持…")))
                        time.sleep(0.22)

                threading.Thread(target=emoji_crawl, daemon=True).start()
                # 等待表情全部就绪（带超时保护，避免无限等待）
                self._emoji_done_event.wait(20)
                self._emoji_crawl_active = False
            else:
                self.event_queue.put(("log", "快速启动：未加载表情图片。"))
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
                seeds = wxdecrypt._scan_emoticon_seeds(pid)
                log(f"扫描到 {len(seeds)} 个候选，正在验证…")
        except Exception:
            pass

        def setup_emoticon():
            try:
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
            for s, count in items:
                if self.session_tree.exists(s.username):
                    continue
                self.avatar_store.ensure(s.username, s.nick_name or "")
                photo = self.avatar_store.get(s.username)
                self.session_tree.insert(
                    node, "end", iid=s.username,
                    text=s.nick_name or s.username,
                    image=photo,
                    values=(count if count else "", (s.time or "")[:10]))

    def filter_sessions(self):
        keyword = self.session_search.get().strip().lower()
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
        """使用中操作的进度条弹窗（切换会话/搜索/导出等）。"""
        self._close_work_progress()  # 防重入：先关掉旧弹窗
        dlg = tk.Toplevel(self)
        dlg.title(title)
        dlg.geometry("320x110")
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.attributes("-topmost", True)
        dlg.protocol("WM_DELETE_WINDOW", lambda: None)
        dlg.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - 320) // 2
        y = self.winfo_rooty() + (self.winfo_height() - 110) // 2
        dlg.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        tk.Label(dlg, text=status_text, font=("Microsoft YaHei", 11)
                 ).pack(pady=(18, 8))
        bar = ttk.Progressbar(dlg, mode="indeterminate", length=260)
        bar.pack(padx=30)
        bar.start(10)
        self._work_progress = (dlg, bar)

    def _close_work_progress(self):
        wp = getattr(self, "_work_progress", None)
        if wp:
            dlg, bar = wp
            try:
                bar.stop()
            except Exception:
                pass
            try:
                if dlg.winfo_exists():
                    dlg.destroy()
            except Exception:
                pass
        self._work_progress = None

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
        """渲染会话消息（全量渲染，图片/表情缺失时占位并后台补图）。"""
        self.current_msgs = msgs
        self._rendered_talker = self.current_talker
        c = self.chat_canvas
        c.delete("all")
        canvas_w = max(c.winfo_width(), 400)
        wrap_limit = int(canvas_w * 0.62)
        y = 10
        last_date = ""
        for idx, m in enumerate(msgs):
            tag = f"msg_{idx}"
            day = (m.time or "")[:10]
            if day and day != last_date:
                last_date = day
                c.create_text(canvas_w / 2, y, text=f"—— {day} ——",
                              font=self.f_date, fill="#999999")
                y += 26
            if m.type == MSG_TYPE_SYSTEM:
                for ln in wrap_lines(m.content or "", self.f_system, canvas_w - 120):
                    c.create_text(canvas_w / 2, y, text=ln, font=self.f_system, fill="#888888")
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
                              fill="#576b95", anchor="nw", tags=(tag,))
            if side == "self":
                self._grad_bubble(c, x1, by, x1 + bubble_w, by + bubble_h, 8,
                                  THEME["bubble_self_top"],
                                  THEME["bubble_self_bottom"],
                                  tags=(tag,))
            else:
                self._grad_bubble(c, x1, by, x1 + bubble_w, by + bubble_h, 8,
                                  "#FFFFFF", "#F0F4FA",
                                  outline=THEME["bubble_border"], tags=(tag,))
            tx = x1 + 15
            ty = by + 6
            for ln in lines:
                c.create_text(tx, ty, text=ln, font=self.f_content,
                              fill="#111111", anchor="nw", tags=(tag,))
                ty += line_h
            if side == "self":
                c.create_text(x1 + bubble_w, by + bubble_h + 2, text=time_str,
                              font=self.f_time, fill="#b0b0b0", anchor="ne", tags=(tag,))
            else:
                c.create_text(x1, by + bubble_h + 2, text=time_str,
                              font=self.f_time, fill="#b0b0b0", anchor="nw", tags=(tag,))
            y = by + bubble_h + 20
        c.configure(scrollregion=(0, 0, canvas_w, y + 8))
        c.yview_moveto(1.0)

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
        """Frutiger 风格渐变气泡：用多条细横线模拟垂直渐变（水润感）。"""
        def hex2rgb(h):
            return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)
        r1, g1, b1 = hex2rgb(c_top)
        r2, g2, b2 = hex2rgb(c_bottom)
        h = max(1, int(y2 - y1))
        # 先画纯色底（避免圆角间缝隙）
        c.create_polygon(
            [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
             x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1],
            smooth=True, fill=c_bottom, outline="", tags=tags)
        # 渐变细线（每 2px 一条）
        step = 2
        for i in range(0, h, step):
            t = i / max(1, h - 1)
            color = "#%02x%02x%02x" % (
                int(r1 + (r2 - r1) * t),
                int(g1 + (g2 - g1) * t),
                int(b1 + (b2 - b1) * t))
            # 只在非圆角区域画（左右各留 r 的圆角半径）
            c.create_rectangle(x1 + 1, y1 + i, x2 - 1, y1 + i + step,
                               fill=color, outline="", tags=tags)

    def _on_canvas_resize(self, _event=None):
        if self._resize_pending:
            return
        self._resize_pending = True
        self.after(250, self._do_resize_redraw)

    def _do_resize_redraw(self):
        self._resize_pending = False
        if self.current_talker and self._rendered_talker == self.current_talker \
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
        top.geometry("520x360")
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
        progress.geometry("300x90")
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
        top.geometry("380x430")
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


def main():
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
    app = ChatViewer()
    app.mainloop()


if __name__ == "__main__":
    main()
