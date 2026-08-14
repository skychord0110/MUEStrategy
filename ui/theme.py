"""コントロールパネルの配色とフォント、および共通の小さなウィジェット。

配色の役割分担（守ること）:
  緑     … 接続OK・稼働の正常・評価損益プラス
  橙     … 「変更が未反映」などの注意喚起。稼働を止める操作ではない
  赤     … 実弾（自動売買）に関わる領域と、停止・緊急停止・評価損益マイナス
  青     … チェックボックスのON（状態そのもので、良し悪しの意味は持たせない）

Tkinterのウィジェットは角丸・グラデーションを描けないため、
区切りはすべて1pxの罫線（Frame）で表現している。
アイコンフォントは使わない（環境によって豆腐（□）になるため）。
記号は ● ■ ▶ ↻ ⚠ ✓ のみを使う。
"""
import tkinter as tk
from tkinter import font as tkfont

# ── 配色 ──────────────────────────────────────────────────────────
BG          = "#141414"   # ウィンドウの地
PANEL       = "#1C1C1C"   # セクションの地
PANEL_ALT   = "#262626"   # チップ・カードの地
PANEL_DEEP  = "#171717"   # 一段沈めた領域
BORDER      = "#2E2E2E"   # 罫線
BORDER_LT   = "#3C3C3C"   # ボタンの枠
HOVER       = "#303030"

TEXT        = "#E8E8E8"
TEXT_DIM    = "#8A8A8A"
TEXT_MUTE   = "#5E5E5E"

GREEN       = "#4ADE80"
GREEN_BG    = "#14351F"
GREEN_DIM   = "#6E9C80"
GREEN_LINE  = "#2E6B45"   # 「起動」ボタンの枠

ORANGE      = "#F0A040"
ORANGE_BG   = "#5A3208"   # 未反映バンドの地
ORANGE_CHIP = "#43260A"   # 未反映チップの地

RED         = "#F0605E"
RED_SOLID   = "#E5484D"   # 緊急停止ボタン
RED_HOVER   = "#F05A5F"
RED_BG      = "#351515"

BLUE        = "#4A9EFF"   # チェックON

LOG_BG      = "#0D0D0D"

# ── フォント ──────────────────────────────────────────────────────
_UI_CANDIDATES = ("Yu Gothic UI", "Meiryo UI", "Meiryo", "MS UI Gothic")
_MONO_CANDIDATES = ("Consolas", "Cascadia Mono", "MS Gothic", "Courier New")

FONTS = {}


def init_fonts(root: tk.Misc) -> dict:
    """実際に入っているフォントから選ぶ（root生成後に一度だけ呼ぶ）。"""
    available = set(tkfont.families(root))

    def pick(cands, fallback):
        for c in cands:
            if c in available:
                return c
        return fallback

    ui = pick(_UI_CANDIDATES, "TkDefaultFont")
    mono = pick(_MONO_CANDIDATES, "TkFixedFont")
    FONTS.update({
        "title":   (ui, 15, "bold"),
        "sub":     (ui, 10),
        "section": (ui, 9),
        "label":   (ui, 9),
        "body":    (ui, 10),
        "bodyb":   (ui, 10, "bold"),
        "small":   (ui, 8),
        "num":     (mono, 11),
        "numb":    (mono, 12, "bold"),
        "nums":    (mono, 9),
        "log":     (mono, 9),
    })
    return FONTS


# ── 共通の小さなウィジェット ────────────────────────────────────────
def hsep(parent, color=BORDER):
    return tk.Frame(parent, bg=color, height=1)


def vsep(parent, color=BORDER):
    return tk.Frame(parent, bg=color, width=1)


def section_label(parent, text, bg=PANEL):
    return tk.Label(parent, text=text, bg=bg, fg=TEXT_DIM,
                    font=FONTS["section"], anchor="w")


class FlatButton(tk.Frame):
    """1px枠のボタン。tk.Buttonでは枠色と背景を細かく指定できないためLabelで作る。"""

    def __init__(self, parent, text, command=None, *, fg=TEXT, border=BORDER_LT,
                 bg=PANEL, solid=None, font=None, pady=9, padx=12, width=None):
        super().__init__(parent, bg=border)
        self._command = command
        self._enabled = True
        self._bg = solid or bg
        self._hover = RED_HOVER if solid else HOVER
        self._fg = "#FFFFFF" if solid else fg
        self._solid = solid is not None
        self._border = border
        self.inner = tk.Label(self, text=text, bg=self._bg, fg=self._fg,
                              font=font or FONTS["body"], pady=pady, padx=padx,
                              cursor="hand2")
        if width:
            self.inner.configure(width=width)
        self.inner.pack(fill="both", expand=True, padx=1, pady=1)
        for w in (self, self.inner):
            w.bind("<Button-1>", self._click)
            w.bind("<Enter>", self._enter)
            w.bind("<Leave>", self._leave)

    def _click(self, _e=None):
        if self._enabled and self._command:
            self._command()

    def _enter(self, _e=None):
        if self._enabled:
            self.inner.configure(bg=self._hover)

    def _leave(self, _e=None):
        if self._enabled:
            self.inner.configure(bg=self._bg)

    def set_text(self, text):
        self.inner.configure(text=text)

    def set_accent(self, color=None):
        """枠と文字の色を一時的に変える（未保存の入力を橙で示す用途）。
        None を渡すと元の色に戻る。"""
        if not hasattr(self, "_base_fg"):
            self._base_fg, self._base_border = self._fg, self._border
        self._fg = color or self._base_fg
        self._border = color or self._base_border
        if self._enabled:
            self.inner.configure(fg=self._fg)
            self.configure(bg=self._border)

    def set_enabled(self, on: bool):
        """無効時は文字と枠を沈める（押しても何も起きないことが見て分かるように）。"""
        self._enabled = bool(on)
        if self._enabled:
            self.inner.configure(fg=self._fg, bg=self._bg, cursor="hand2")
            self.configure(bg=self._border)
        else:
            self.inner.configure(fg=TEXT_MUTE, bg=PANEL_DEEP if not self._solid else "#3A2020",
                                 cursor="arrow")
            self.configure(bg=BORDER)


class Chip(tk.Frame):
    """ストラテジーのON/OFFチップ。チェックボックス＋ラベルの塊全体が押せる。

    見た目は3状態:
      ON        … 地=PANEL_ALT / チェック=青 / 文字=明るい
      OFF       … 地=PANEL     / チェック=空 / 文字=沈む
      未反映     … 地=橙        / 文字=橙（ランナー再起動が必要な変更が入っている）
    """

    def __init__(self, parent, text, value: bool, command=None, sub: str = ""):
        """sub: ラベルの右側に小さく添える補足（損切り/利確幅など）。"""
        super().__init__(parent, bg=BORDER)
        self._command = command
        self.value = bool(value)
        self.dirty = False
        self._enabled = True

        self.inner = tk.Frame(self, bg=PANEL_ALT)
        self.inner.pack(fill="both", expand=True, padx=1, pady=1)
        self.box = tk.Label(self.inner, text="", width=2, font=FONTS["small"])
        self.box.pack(side="left", padx=(10, 8), pady=8)
        # 補足は右端に固定し、名前が長くても折り返さないようにする
        self.sub = tk.Label(self.inner, text=sub, bg=PANEL_ALT, fg=TEXT_MUTE,
                            font=FONTS["small"], anchor="e")
        self.sub.pack(side="right", pady=8, padx=(6, 10))
        self.text = tk.Label(self.inner, text=text, bg=PANEL_ALT, fg=TEXT,
                             font=FONTS["body"], anchor="w")
        self.text.pack(side="left", fill="x", expand=True, pady=8)

        for w in (self, self.inner, self.box, self.text, self.sub):
            w.bind("<Button-1>", self._click)
            w.configure(cursor="hand2")
        self._paint()

    def set_sub(self, text: str):
        if self.sub.cget("text") != text:
            self.sub.configure(text=text)

    def _click(self, _e=None):
        if not self._enabled:
            return
        if self._command:
            self._command(self, not self.value)

    def set_value(self, value: bool, dirty: bool = None):
        self.value = bool(value)
        if dirty is not None:
            self.dirty = bool(dirty)
        self._paint()

    def set_dirty(self, dirty: bool):
        self.dirty = bool(dirty)
        self._paint()

    def set_enabled(self, on: bool):
        self._enabled = bool(on)
        cur = "hand2" if on else "arrow"
        for w in (self, self.inner, self.box, self.text, self.sub):
            w.configure(cursor=cur)
        self._paint()

    def _paint(self):
        if self.dirty:
            bg, fg, border, sub = ORANGE_CHIP, ORANGE, "#6B4310", "#B8823C"
        elif self.value:
            bg, fg, border, sub = PANEL_ALT, TEXT, BORDER, TEXT_DIM
        else:
            bg, fg, border, sub = PANEL, TEXT_MUTE, BORDER, "#4A4A4A"
        if not self._enabled:
            fg = TEXT_MUTE
        self.configure(bg=border)
        self.inner.configure(bg=bg)
        self.text.configure(bg=bg, fg=fg)
        self.sub.configure(bg=bg, fg=sub)
        if self.value:
            self.box.configure(bg=BLUE if self._enabled else "#33556E",
                               fg="#FFFFFF", text="✓")
        else:
            self.box.configure(bg="#3A3A3A", fg="#3A3A3A", text=" ")
