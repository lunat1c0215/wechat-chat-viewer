#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Frutiger Aero 轻量组件库（纯 tkinter + Pillow，无第三方 UI 框架）。

- 渐变/噪点背景（Pillow 1px 列拉伸，C 层完成，不再 Python 逐像素）
- 玻璃质感圆角按钮 GButton
- 圆角细进度条 SlimProgress（缓动动画，可爬行模式）
- 极轻噪点纹理，避免大面积纯色的"死板感"
"""

from __future__ import annotations

import io
import random
import tkinter as tk

from PIL import Image, ImageChops, ImageDraw

_PHOTO_CACHE: dict = {}


def _hex_rgb(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _photo(img: Image.Image, key) -> tk.PhotoImage:
    cached = _PHOTO_CACHE.get(key)
    if cached is not None:
        return cached
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    photo = tk.PhotoImage(data=buf.getvalue())
    if len(_PHOTO_CACHE) > 96:
        _PHOTO_CACHE.clear()
    _PHOTO_CACHE[key] = photo
    return photo


def _vgrad(w: int, h: int, c1: str, c2: str) -> Image.Image:
    """垂直渐变：先生成 1×h 的列，再拉伸到 w×h（C 层插值，毫秒级）。"""
    h = max(1, int(h))
    r1, g1, b1 = _hex_rgb(c1)
    r2, g2, b2 = _hex_rgb(c2)
    col = Image.new("RGB", (1, h))
    px = col.load()
    for y in range(h):
        t = y / max(1, h - 1)
        px[0, y] = (int(r1 + (r2 - r1) * t),
                    int(g1 + (g2 - g1) * t),
                    int(b1 + (b2 - b1) * t))
    return col.resize((max(1, int(w)), h), Image.NEAREST)


def _noise_layer(w: int, h: int, strength: int = 3) -> Image.Image:
    """64×64 随机噪点 tile 拉伸 → 极轻纹理（±strength 色阶）。"""
    rnd = random.Random(20260821)
    tile = bytes(rnd.randrange(0, 255) for _ in range(64 * 64))
    img = Image.frombytes("L", (64, 64), tile).resize((max(1, int(w)),
                                                       max(1, int(h))),
                                                      Image.BILINEAR)
    return img.point(lambda v: 128 + (v - 128) // max(1, strength))


def gradient_photo(w: int, h: int, c1: str, c2: str,
                   noise: int = 0, gloss: bool = False) -> tk.PhotoImage:
    """带缓存的渐变 PhotoImage；noise>0 叠加轻噪点，gloss=True 加顶部玻璃高光。"""
    w, h = max(1, int(w)), max(1, int(h))
    key = ("grad", w, h, c1, c2, noise, gloss)
    cached = _PHOTO_CACHE.get(key)
    if cached is not None:
        return cached
    img = _vgrad(w, h, c1, c2).convert("RGB")
    if noise:
        n = _noise_layer(w, h, noise).convert("L")
        img = ImageChops.blend(img, Image.merge("RGB", (n, n, n)),
                               0.035 if noise >= 2 else 0.02)
    if gloss:
        gh = max(2, int(h * 0.52))
        white = Image.new("RGB", (w, gh), "#FFFFFF")
        alpha = Image.new("L", (1, gh))
        ap = alpha.load()
        for y in range(gh):
            ap[0, y] = int(78 * (1 - y / max(1, gh - 1)) ** 1.4)
        mask = alpha.resize((w, gh))
        img.paste(white, (0, 0), mask)
    return _photo(img, key)


def get_grad(w: int, h: int, c1: str, c2: str,
             noise: int = 0, gloss: bool = False) -> tk.PhotoImage:
    return gradient_photo(w, h, c1, c2, noise=noise, gloss=gloss)


def rrect_points(x1, y1, x2, y2, r):
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    return [
        x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1,
        x2, y1 + r, x2, y1 + r, x2, y2 - r, x2, y2 - r,
        x2, y2, x2 - r, y2, x2 - r, y2, x1 + r, y2,
        x1 + r, y2, x1, y2, x1, y2 - r, x1, y2 - r,
        x1, y1 + r, x1, y1 + r, x1, y1,
    ]


def draw_rrect(canvas: tk.Canvas, x1, y1, x2, y2, r, **kw):
    return canvas.create_polygon(rrect_points(x1, y1, x2, y2, r),
                                 smooth=True, **kw)


class GButton(tk.Canvas):
    """玻璃质感圆角按钮：primary（主蓝）/ ghost（浅色）/ danger（取消红）。"""

    _KINDS = {
        "primary": {
            "top": "#5C97F6", "bottom": "#2F6FE0", "border": "#2A63C9",
            "text": "#FFFFFF",
            "hover": ("#6EA6FF", "#3B7DEE"), "press": ("#2A63C9", "#2255B4"),
        },
        "ghost": {
            "top": "#FFFFFF", "bottom": "#F0F6FE", "border": "#C9D8EC",
            "text": "#2C4A6B",
            "hover": ("#F8FBFF", "#E4EFFB"), "press": ("#DDE9F8", "#D2E2F5"),
        },
        "danger": {
            "top": "#FF8A80", "bottom": "#E5544B", "border": "#C9443B",
            "text": "#FFFFFF",
            "hover": ("#FF9A91", "#EF6259"), "press": ("#D24840", "#B93B33"),
        },
    }

    def __init__(self, parent, text: str, command=None, kind: str = "ghost",
                 width: int = 108, height: int = 34, font=None, radius: int = 9):
        bg = parent.cget("bg") if "bg" in parent.keys() else "#FFFFFF"
        super().__init__(parent, width=width, height=height, bd=0,
                         highlightthickness=0, bg=bg, cursor="hand2")
        self._text = text
        self._command = command
        self._kind = kind if kind in self._KINDS else "ghost"
        self._gw, self._gh, self._gr = width, height, radius
        self._font = font or ("Microsoft YaHei", 10)
        self._enabled = True
        self._hover = False
        self._pressed = False
        self._img = None
        self._text_id = None
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.refresh()

    # ---- 状态 ----
    def set_enabled(self, enabled: bool):
        self._enabled = bool(enabled)
        self.configure(cursor="hand2" if enabled else "arrow")
        self.refresh()

    def set_text(self, text: str):
        if text != self._text:
            self._text = text
            if self._text_id:
                self.itemconfigure(self._text_id, text=text)

    def set_kind(self, kind: str):
        if kind in self._KINDS and kind != self._kind:
            self._kind = kind
            self.refresh()

    # ---- 绘制 ----
    def _palette(self):
        base = self._KINDS[self._kind]
        if not self._enabled:
            return {"top": "#F4F7FB", "bottom": "#E8EEF6",
                    "border": "#D8E2EF", "text": "#A9B7C9"}
        top, bottom = base["top"], base["bottom"]
        if self._pressed:
            top, bottom = base["press"]
        elif self._hover:
            top, bottom = base["hover"]
        return {"top": top, "bottom": bottom,
                "border": base["border"], "text": base["text"]}

    def _build_image(self):
        p = self._palette()
        w, h = self._gw, self._gh
        key = ("btn", w, h, self._gr, p["top"], p["bottom"], p["border"])
        cached = _PHOTO_CACHE.get(key)
        if cached is not None:
            return cached
        img = _vgrad(w, h, p["top"], p["bottom"]).convert("RGBA")
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, w - 1, h - 1], radius=self._gr, fill=255)
        # 顶部玻璃高光
        gh = max(2, int(h * 0.5))
        grad = Image.new("L", (1, gh))
        gp = grad.load()
        for y in range(gh):
            gp[0, y] = int(96 * (1 - y / max(1, gh - 1)) ** 1.5)
        gloss_mask = grad.resize((w, gh))
        gloss_full = Image.new("L", (w, h), 0)
        gloss_full.paste(gloss_mask, (0, 0))
        gloss_full = ImageChops.multiply(gloss_full, mask)
        img.paste(Image.new("RGBA", (w, h), (255, 255, 255, 255)),
                  (0, 0), gloss_full)
        img.putalpha(mask)
        ImageDraw.Draw(img).rounded_rectangle(
            [0, 0, w - 1, h - 1], radius=self._gr,
            outline=p["border"], width=1)
        return _photo(img, key)

    def refresh(self):
        self.delete("all")
        self._img = self._build_image()
        self.create_image(0, 0, anchor="nw", image=self._img)
        p = self._palette()
        self._text_id = self.create_text(
            self._gw / 2, self._gh / 2 + (1 if self._pressed else 0),
            text=self._text, fill=p["text"], font=self._font)

    # ---- 事件 ----
    def _on_enter(self, _e=None):
        if not self._enabled:
            return
        self._hover = True
        self.refresh()

    def _on_leave(self, _e=None):
        self._hover = False
        self._pressed = False
        self.refresh()

    def _on_press(self, _e=None):
        if not self._enabled:
            return
        self._pressed = True
        self.refresh()

    def _on_release(self, _e=None):
        if not self._enabled:
            return
        was_pressed = self._pressed
        self._pressed = False
        self.refresh()
        if was_pressed and self._command:
            try:
                self._command()
            except Exception:
                import traceback
                traceback.print_exc()


class SlimProgress(tk.Canvas):
    """圆角细进度条：缓动逼近目标值，支持爬行模式与百分比文字。"""

    def __init__(self, parent, width: int = 420, height: int = 10,
                 bg: str = "#FFFFFF", track: str = "#E4EDF9",
                 fill: str = "#2F6FE0", light: str = "#7FB0FF"):
        super().__init__(parent, width=width, height=height, bd=0,
                         highlightthickness=0, bg=bg)
        self._sw = width
        self._sh = height
        self._track = track
        self._fill = fill
        self._light = light
        self._value = 0.0     # 当前显示值
        self._target = 0.0
        self._crawl = None    # (lo, hi, step)
        self._job = None
        self.bind("<Configure>", self._on_resize)
        self._draw()

    # ---- 对外 ----
    def set(self, value: float, animate: bool = True):
        self._target = max(0.0, min(100.0, float(value)))
        if not animate:
            self._value = self._target
            self._draw()
            return
        if self._job is None:
            self._tick()

    def start_crawl(self, lo: float = 2, hi: float = 24, step: float = 0.55):
        """未知时长阶段：从 lo 缓慢逼近 hi，不假装完成。"""
        self.stop_crawl()
        self.set(lo, animate=False)
        self._crawl = [lo, hi, step]
        if self._job is None:
            self._tick()

    def stop_crawl(self, keep_value: bool = True):
        self._crawl = None
        if not keep_value:
            self.set(0, animate=False)

    def value(self) -> float:
        return self._value

    # ---- 绘制 ----
    def _on_resize(self, event):
        if event.width > 10 and abs(event.width - self._sw) > 1:
            self._sw = event.width
            self._draw()

    def _draw(self):
        self.delete("all")
        w, h = max(10, self._sw), self._sh
        r = h / 2
        draw_rrect(self, 1, 1, w - 1, h - 1, r, fill=self._track, outline="")
        fw = (w - 2) * self._value / 100.0
        if fw > 2:
            draw_rrect(self, 1, 1, 1 + max(fw, h - 2), h - 1, r,
                       fill=self._fill, outline="")
            # 顶部一条更亮的细线，玻璃质感
            self.create_line(1 + r, 2.5, 1 + max(fw - r, r), 2.5,
                             fill=self._light, width=1)

    def _tick(self):
        self._job = None
        if self._crawl:
            lo, hi, step = self._crawl
            if self._target < hi:
                self._target = min(hi, self._target + step)
        diff = self._target - self._value
        if abs(diff) > 0.25:
            self._value += diff * 0.28
            self._draw()
            self._job = self.after(16, self._tick)
        elif self._crawl and self._target < self._crawl[1] - 0.5:
            self._value = self._target
            self._draw()
            self._job = self.after(90, self._tick)
        else:
            self._value = self._target
            self._draw()
            if self._crawl and self._target < self._crawl[1] - 0.5:
                self._job = self.after(90, self._tick)

    def destroy(self):
        if self._job:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        super().destroy()
