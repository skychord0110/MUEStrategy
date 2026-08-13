"""MUEStrategy コントロールパネル

「コントロールパネル.bat」をダブルクリックすると起動する。

できること:
  - 監視銘柄の入れ替え（extracted_stocks/ の最新CSVの上位50銘柄）
  - 検知ストラテジー／AI仮想売買のON/OFF
  - 自動売買の enabled / dry_run の切り替え（確認付き）
  - ランナーの起動・停止・再起動
  - 買付余力・建玉・評価損益の表示

【口座情報の出どころ】
このUIは kabuステーションAPI のトークンを一切発行しない。
公式リファレンス POST /token に「別のトークンが新たに発行された時」に既存トークンが
無効になると明記されており、UIが認証すると稼働中ランナーの発注が失敗しうるため。
買付余力・建玉・評価損益は、ランナーが書き出す
strategies/runner/state/account.json を読んで表示している。
（＝ランナー停止中は「—」になる。これは異常ではない）
"""
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from datetime import datetime
from tkinter import messagebox, simpledialog

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import config_io                                    # noqa: E402
import runner_control                               # noqa: E402
import symbols_update                               # noqa: E402
from theme import (BG, BORDER, BORDER_LT, FONTS, GREEN, GREEN_BG,   # noqa: E402
                   GREEN_DIM, LOG_BG, ORANGE, ORANGE_BG, PANEL, PANEL_ALT,
                   PANEL_DEEP, RED, RED_BG, RED_SOLID, TEXT, TEXT_DIM,
                   TEXT_MUTE, Chip, FlatButton, hsep, init_fonts,
                   section_label, vsep)

ROOT = runner_control.repo_root()
RUNNER_CONFIG = os.path.join(ROOT, "strategies", "runner", "config.yaml")
AUTOTRADE_CONFIG = os.path.join(ROOT, "strategies", "autotrade", "config.yaml")
SYMBOLS_YAML = os.path.join(ROOT, "strategies", "symbols.yaml")
ACCOUNT_JSON = os.path.join(ROOT, "strategies", "runner", "state", "account.json")

KABU_PORTS = {"production": 18080, "demo": 18081}
PROBE_INTERVAL = 5.0
MAX_LOG_LINES = 1500

# 検知ストラテジー（runner/config.yaml）。(表示名, セクション名, configでの位置)
DETECTORS = [
    ("小口売り連続",     "small_lot_sell_detector", ("strategies", "small_lot_sell_detector")),
    ("投げ売り",         "panic_sell_detector",     ("strategies", "panic_sell_detector")),
    ("UNDER急増",        "under_surge_detector",    ("strategies", "under_surge_detector")),
    ("定期買い集め z値", "periodic_buy_zscore",     ("periodic_buy_zscore",)),
    ("旧RSS",            "periodic_buy_rss",        ("periodic_buy_rss",)),
    ("EDINET",           "edinet_holder_monitor",   ("edinet_holder_monitor",)),
]

# AI仮想売買（発注なし）
AI_STRATEGIES = [
    ("午後引け戻り",   "afternoon_reversal", ("strategies", "afternoon_reversal")),
    ("投げ売り反発",   "panic_rebound",      ("strategies", "panic_rebound")),
    ("複合シグナル",   "confluence",         ("strategies", "confluence")),
]

# 実弾で動かす戦略（autotrade/config.yaml の strategies）。
# 自動売買が発注するのはAI戦略のENTRYシグナルを受けたときだけなので、
# 実売買の対象になり得るのはこの3つだけ（AI_STRATEGIES と同じ顔ぶれ）。
AT_STRATEGIES = [
    ("午後引け戻り", "afternoon_reversal"),
    ("投げ売り反発", "panic_rebound"),
    ("複合シグナル", "confluence"),
]

# 資金上限（autotrade/config.yaml の capital）
AT_AMOUNTS = [
    ("使用上限額",   "max_use_amount"),
    ("1銘柄あたり",  "max_amount_per_symbol"),
]

# 全角数字・桁区切り・単位を混ぜて入力されても受け付ける
_AMOUNT_TRANS = str.maketrans("０１２３４５６７８９，", "0123456789,")

_LOGLINE = re.compile(r"^\d{4}-\d{2}-\d{2}\s+(\d{2}:\d{2}:\d{2})[,.]?\d*\s+\[(\w+)\]\s*(.*)$")


def parse_amount(text: str) -> int:
    """「70,000」「７００００」「70000円」などを 70000 にする。"""
    s = str(text).translate(_AMOUNT_TRANS)
    for ch in (",", "円", " ", "　", "\t"):
        s = s.replace(ch, "")
    s = s.strip()
    if not s.isdigit():
        raise ValueError("金額は0以上の整数で入力してください")
    return int(s)


def yen(v, sign=False):
    if v is None:
        return "—"
    s = f"{abs(v):,.0f}"
    if sign:
        return ("+¥" if v >= 0 else "−¥") + s
    return "¥" + s


class ControlPanel:
    def __init__(self, root: tk.Tk):
        self.root = root
        init_fonts(root)
        root.title("MUEStrategy コントロールパネル")
        root.configure(bg=BG)
        root.minsize(880, 620)
        # ノートPCの画面には1040pxが収まらないことがあるので、画面に合わせて縮める。
        # 縮んだぶんはログ欄が吸収する（他のセクションは高さが固定）。
        w = min(920, root.winfo_screenwidth() - 60)
        h = min(1040, root.winfo_screenheight() - 90)
        x = max(0, (root.winfo_screenwidth() - w) // 2)
        y = max(0, (root.winfo_screenheight() - h) // 3)
        root.geometry(f"{w}x{h}+{x}+{y}")

        # Tkのウィジェットはメインスレッドからしか触れない（root.after すら不可）。
        # 別スレッドからの依頼はすべてこのキューに積み、_drain がメインスレッドで実行する。
        self.log_queue = queue.Queue()
        self.ui_queue = queue.Queue()
        self.runner = runner_control.RunnerProcess(on_line=self.log_queue.put)
        self.autoscroll = True
        self.kabu_ok = None            # None=未確認 / True / False
        self.environment = "production"
        self.busy = False              # 起動・停止・更新の実行中
        self.applied = None            # ランナー起動時点の設定（未反映判定の基準）
        self.account = None
        self._cfg_cache = {}
        self._last_probe = 0.0
        self._closing = False

        self._build()
        self._reload_configs(force=True)
        self._read_account()
        self._refresh_ui()     # 最初の_tickを待たずに実状態を描く（一瞬OFFに見えるのを防ぐ）
        self._start_probe_thread()
        self.root.after(120, self._drain)
        self.root.after(200, self._tick)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._append_log("[コントロールパネル] 起動しました。"
                         "ランナーを動かす前に kabuステーション を起動してログインしてください")

    # ══════════════════════════════════════════════════════════════
    # 画面の組み立て
    # ══════════════════════════════════════════════════════════════
    def _build(self):
        card = tk.Frame(self.root, bg=BORDER)
        card.pack(fill="both", expand=True, padx=10, pady=10)
        body = tk.Frame(card, bg=PANEL)
        body.pack(fill="both", expand=True, padx=1, pady=1)
        self.body = body

        self._build_header(body)
        hsep(body).pack(fill="x")
        self._build_status(body)
        hsep(body).pack(fill="x")
        self._build_positions(body)
        self.pos_sep = hsep(body)
        self.pos_sep.pack(fill="x")
        self._build_band(body)
        self._build_runner(body)
        hsep(body).pack(fill="x")
        self._build_strategies(body)
        hsep(body).pack(fill="x")
        self._build_autotrade(body)
        hsep(body).pack(fill="x")
        self._build_log(body)

    # ── ヘッダ ──
    def _build_header(self, parent):
        f = tk.Frame(parent, bg="#202020")
        f.pack(fill="x")
        inner = tk.Frame(f, bg="#202020")
        inner.pack(fill="x", padx=18, pady=12)
        self.hdr_dot = tk.Label(inner, text="●", bg="#202020", fg=TEXT_MUTE,
                                font=FONTS["small"])
        self.hdr_dot.pack(side="left", padx=(0, 10))
        tk.Label(inner, text="MUEStrategy", bg="#202020", fg=TEXT,
                 font=FONTS["title"]).pack(side="left")
        tk.Label(inner, text="コントロールパネル", bg="#202020", fg=TEXT_DIM,
                 font=FONTS["sub"]).pack(side="left", padx=(14, 0))
        self.clock = tk.Label(inner, text="--:--:--", bg="#202020", fg=TEXT_DIM,
                              font=FONTS["num"])
        self.clock.pack(side="right")

    # ── 状態4分割 ──
    def _build_status(self, parent):
        f = tk.Frame(parent, bg=PANEL)
        f.pack(fill="x")
        for c in (0, 2, 4, 6):
            f.columnconfigure(c, weight=1, uniform="status")

        def cell(col, title, bg=PANEL):
            box = tk.Frame(f, bg=bg)
            box.grid(row=0, column=col, sticky="nsew")
            pad = tk.Frame(box, bg=bg)
            pad.pack(fill="both", expand=True, padx=16, pady=10)
            t = tk.Label(pad, text=title, bg=bg, fg=TEXT_DIM,
                         font=FONTS["section"], anchor="w")
            t.pack(fill="x")
            v = tk.Label(pad, text="—", bg=bg, fg=TEXT, font=FONTS["body"], anchor="w")
            v.pack(fill="x", pady=(3, 0))
            return box, pad, t, v

        _, _, _, self.st_kabu = cell(0, "kabuステーション")
        vsep(f).grid(row=0, column=1, sticky="ns")
        _, _, _, self.st_runner = cell(2, "ランナー")
        vsep(f).grid(row=0, column=3, sticky="ns")
        _, _, _, self.st_power = cell(4, "買付余力")
        self.st_power.configure(font=FONTS["num"])
        vsep(f).grid(row=0, column=5, sticky="ns")
        self.pl_box, self.pl_pad, self.pl_title, self.st_pl = cell(6, "評価損益")
        self.st_pl.configure(font=FONTS["numb"])

    # ── 建玉 ──
    def _build_positions(self, parent):
        self.pos_frame = tk.Frame(parent, bg=PANEL)
        self.pos_frame.pack(fill="x")
        self.pos_inner = tk.Frame(self.pos_frame, bg=PANEL)
        self.pos_inner.pack(fill="x", padx=18, pady=10)
        self.pos_title = tk.Label(self.pos_inner, text="建玉 —", bg=PANEL, fg=TEXT_DIM,
                                  font=FONTS["section"], anchor="w")
        self.pos_title.pack(fill="x")
        self.pos_rows = tk.Frame(self.pos_inner, bg=PANEL)
        self.pos_rows.pack(fill="x", pady=(4, 0))
        for c, w in ((0, 3), (1, 1), (2, 1), (3, 1), (4, 1)):
            self.pos_rows.columnconfigure(c, weight=w)

    # ── 未反映バンド（必要なときだけ差し込む） ──
    def _build_band(self, parent):
        self.band = tk.Frame(parent, bg=ORANGE_BG)
        bar = tk.Frame(self.band, bg=ORANGE, width=3)
        bar.pack(side="left", fill="y")
        inner = tk.Frame(self.band, bg=ORANGE_BG)
        inner.pack(fill="x", expand=True, padx=15, pady=11)
        tk.Label(inner, text="⚠", bg=ORANGE_BG, fg=ORANGE,
                 font=FONTS["bodyb"]).pack(side="left", padx=(0, 12))
        self.band_btn = FlatButton(inner, "↻ 停止して再起動", command=self.restart_runner,
                                   fg=ORANGE, border=ORANGE, bg=ORANGE_BG, pady=8, padx=16)
        self.band_btn.pack(side="right")
        txt = tk.Frame(inner, bg=ORANGE_BG)
        txt.pack(side="left", fill="x", expand=True)
        tk.Label(txt, text="変更が未反映です", bg=ORANGE_BG, fg=ORANGE,
                 font=FONTS["bodyb"], anchor="w").pack(fill="x")
        self.band_detail = tk.Label(txt, text="", bg=ORANGE_BG, fg="#D9975A",
                                    font=FONTS["label"], anchor="w", justify="left")
        self.band_detail.pack(fill="x", pady=(2, 0))
        self.band_shown = False

    def _set_band(self, reasons):
        want = bool(reasons)
        if want:
            self.band_detail.configure(
                text="・".join(reasons) + " — 反映するにはランナーを再起動してください")
        if want != self.band_shown:
            if want:
                self.band.pack(fill="x", before=self.runner_sec)
            else:
                self.band.pack_forget()
            self.band_shown = want

    # ── ランナー操作・銘柄リスト ──
    def _build_runner(self, parent):
        self.runner_sec = tk.Frame(parent, bg=PANEL)
        self.runner_sec.pack(fill="x")
        f = self.runner_sec
        f.columnconfigure(0, weight=1, uniform="rn")
        f.columnconfigure(2, weight=1, uniform="rn")

        left = tk.Frame(f, bg=PANEL)
        left.grid(row=0, column=0, sticky="nsew")
        lp = tk.Frame(left, bg=PANEL)
        lp.pack(fill="both", expand=True, padx=18, pady=(12, 14))
        section_label(lp, "ランナー").pack(fill="x", pady=(0, 8))
        row = tk.Frame(lp, bg=PANEL)
        row.pack(fill="x")
        row.columnconfigure(0, weight=1, uniform="btn")
        row.columnconfigure(2, weight=1, uniform="btn")
        self.btn_start = FlatButton(row, "▶ 起動", command=self.start_runner)
        self.btn_start.grid(row=0, column=0, sticky="ew")
        tk.Frame(row, bg=PANEL, width=12).grid(row=0, column=1)
        self.btn_stop = FlatButton(row, "■ 停止", command=self.stop_runner,
                                   fg=RED, border="#5A2A2A")
        self.btn_stop.grid(row=0, column=2, sticky="ew")

        vsep(f).grid(row=0, column=1, sticky="ns")

        right = tk.Frame(f, bg=PANEL)
        right.grid(row=0, column=2, sticky="nsew")
        rp = tk.Frame(right, bg=PANEL)
        rp.pack(fill="both", expand=True, padx=18, pady=(12, 14))
        self.sym_label = section_label(rp, "銘柄リスト —")
        self.sym_label.pack(fill="x", pady=(0, 8))
        self.btn_symbols = FlatButton(rp, "↻ 最新CSVで更新", command=self.update_symbols)
        self.btn_symbols.pack(fill="x")

    # ── ストラテジー ──
    def _build_strategies(self, parent):
        f = tk.Frame(parent, bg=PANEL)
        f.pack(fill="x")
        p = tk.Frame(f, bg=PANEL)
        p.pack(fill="x", padx=18, pady=(14, 16))
        self.chips = {}

        section_label(p, "検知ストラテジー").pack(fill="x", pady=(0, 9))
        self._chip_grid(p, DETECTORS, cols=3)
        section_label(p, "AI仮想売買（発注なし）").pack(fill="x", pady=(14, 9))
        self._chip_grid(p, AI_STRATEGIES, cols=3)

    def _chip_grid(self, parent, items, cols=3):
        grid = tk.Frame(parent, bg=PANEL)
        grid.pack(fill="x")
        for c in range(cols):
            grid.columnconfigure(c * 2, weight=1, uniform="chip")
        for i, (label, key, _path) in enumerate(items):
            r, c = divmod(i, cols)
            chip = Chip(grid, label, False, command=self._on_chip)
            chip.grid(row=r * 2, column=c * 2, sticky="ew")
            chip.key = key
            self.chips[key] = chip
            if c * 2 + 1 < cols * 2 - 1:
                tk.Frame(grid, bg=PANEL, width=12).grid(row=r * 2, column=c * 2 + 1)
            if r * 2 + 1 <= (len(items) - 1) // cols * 2:
                tk.Frame(grid, bg=PANEL, height=10).grid(row=r * 2 + 1, column=c * 2)

    # ── 自動売買 ──
    def _build_autotrade(self, parent):
        wrap = tk.Frame(parent, bg=PANEL)
        wrap.pack(fill="x")
        tk.Frame(wrap, bg="#FF4D4D", width=3).pack(side="left", fill="y")
        f = tk.Frame(wrap, bg=PANEL)
        f.pack(fill="x", expand=True, padx=15, pady=(13, 15))

        head = tk.Frame(f, bg=PANEL)
        head.pack(fill="x", pady=(0, 10))
        tk.Label(head, text="自動売買 — 実弾", bg=PANEL, fg=RED,
                 font=FONTS["bodyb"]).pack(side="left")
        self.at_badge = tk.Label(head, text="—", bg=GREEN_BG, fg=GREEN,
                                 font=FONTS["small"], padx=10, pady=3)
        self.at_badge.pack(side="left", padx=(14, 0))

        cards = tk.Frame(f, bg=PANEL)
        cards.pack(fill="x")
        cards.columnconfigure(0, weight=1, uniform="at")
        cards.columnconfigure(2, weight=1, uniform="at")
        self.at_enabled_val = self._toggle_card(
            cards, 0, "enabled", lambda: self.toggle_autotrade("enabled"))
        tk.Frame(cards, bg=PANEL, width=14).grid(row=0, column=1)
        self.at_dryrun_val = self._toggle_card(
            cards, 2, "dry_run", lambda: self.toggle_autotrade("dry_run"))

        # ── 実売買に使う戦略 ──
        section_label(f, "実売買に使う戦略（チェックした戦略だけが実際に発注する）").pack(
            fill="x", pady=(15, 8))
        sg = tk.Frame(f, bg=PANEL)
        sg.pack(fill="x")
        self.at_chips = {}
        for c in range(len(AT_STRATEGIES)):
            sg.columnconfigure(c * 2, weight=1, uniform="atchip")
        for i, (label, key) in enumerate(AT_STRATEGIES):
            chip = Chip(sg, label, False, command=self._on_at_chip)
            chip.grid(row=0, column=i * 2, sticky="ew")
            chip.key = key
            self.at_chips[key] = chip
            if i < len(AT_STRATEGIES) - 1:
                tk.Frame(sg, bg=PANEL, width=12).grid(row=0, column=i * 2 + 1)

        # ── 資金上限 ──
        section_label(f, "資金上限（残高を参照し、この金額を超えない範囲で数量を決める）").pack(
            fill="x", pady=(15, 8))
        ag = tk.Frame(f, bg=PANEL)
        ag.pack(fill="x")
        ag.columnconfigure(0, weight=1, uniform="amt")
        ag.columnconfigure(2, weight=1, uniform="amt")
        self.amount_entries = {}
        self._amount_shown = {}
        for i, (label, key) in enumerate(AT_AMOUNTS):
            self.amount_entries[key] = self._amount_card(ag, i * 2, label)
        tk.Frame(ag, bg=PANEL, width=14).grid(row=0, column=1)
        self.btn_amount = FlatButton(ag, "保存", command=self.save_amounts,
                                     pady=20, padx=18)
        self.btn_amount.grid(row=0, column=4, padx=(14, 0), sticky="ns")

        self.btn_panic = FlatButton(f, "緊急停止", command=self.emergency_stop,
                                    solid=RED_SOLID, border=RED_SOLID,
                                    font=FONTS["bodyb"], pady=12)
        self.btn_panic.pack(fill="x", pady=(14, 0))

    def _toggle_card(self, parent, col, name, command):
        outer = tk.Frame(parent, bg=BORDER)
        outer.grid(row=0, column=col, sticky="ew")
        inner = tk.Frame(outer, bg=PANEL_ALT)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        pad = tk.Frame(inner, bg=PANEL_ALT)
        pad.pack(fill="x", padx=14, pady=11)
        FlatButton(pad, "切替", command=command, bg=PANEL_ALT,
                   font=FONTS["label"], pady=6, padx=14).pack(side="right")
        txt = tk.Frame(pad, bg=PANEL_ALT)
        txt.pack(side="left", fill="x", expand=True)
        tk.Label(txt, text=name, bg=PANEL_ALT, fg=TEXT_DIM,
                 font=FONTS["label"], anchor="w").pack(fill="x")
        val = tk.Label(txt, text="—", bg=PANEL_ALT, fg=TEXT,
                       font=FONTS["bodyb"], anchor="w")
        val.pack(fill="x", pady=(2, 0))
        return val

    def _amount_card(self, parent, col, label):
        """金額の入力欄。入力できることが見て分かるよう、枠付きの暗い入力箱にする。"""
        outer = tk.Frame(parent, bg=BORDER)
        outer.grid(row=0, column=col, sticky="ew")
        inner = tk.Frame(outer, bg=PANEL_ALT)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        pad = tk.Frame(inner, bg=PANEL_ALT)
        pad.pack(fill="x", padx=14, pady=11)
        tk.Label(pad, text=label, bg=PANEL_ALT, fg=TEXT_DIM,
                 font=FONTS["label"], anchor="w").pack(fill="x")
        row = tk.Frame(pad, bg=PANEL_ALT)
        row.pack(fill="x", pady=(4, 0))
        tk.Label(row, text="円", bg=PANEL_ALT, fg=TEXT_DIM,
                 font=FONTS["label"]).pack(side="right", padx=(8, 0))
        box = tk.Frame(row, bg=BORDER_LT)
        box.pack(side="left", fill="x", expand=True)
        ent = tk.Entry(box, bg=PANEL_DEEP, fg=TEXT, font=FONTS["num"], bd=0,
                       highlightthickness=0, insertbackground=TEXT, justify="right")
        ent.pack(fill="x", padx=1, pady=1, ipady=4, ipadx=8)
        ent.bind("<KeyRelease>", lambda _e: self._refresh_amount_button())
        ent.bind("<Return>", lambda _e: self.save_amounts())
        return ent

    # ── ログ ──
    def _build_log(self, parent):
        f = tk.Frame(parent, bg=PANEL)
        f.pack(fill="both", expand=True)
        p = tk.Frame(f, bg=PANEL)
        p.pack(fill="both", expand=True, padx=18, pady=(12, 16))

        head = tk.Frame(p, bg=PANEL)
        head.pack(fill="x", pady=(0, 8))
        tk.Label(head, text="ログ", bg=PANEL, fg=TEXT_DIM,
                 font=FONTS["section"]).pack(side="left")
        self.scroll_lbl = tk.Label(head, text="✓ 自動スクロール", bg=PANEL, fg=TEXT_DIM,
                                   font=FONTS["section"], cursor="hand2")
        self.scroll_lbl.pack(side="right")
        self.scroll_lbl.bind("<Button-1>", self._toggle_autoscroll)

        box = tk.Frame(p, bg=BORDER)
        box.pack(fill="both", expand=True)
        holder = tk.Frame(box, bg=LOG_BG)
        holder.pack(fill="both", expand=True, padx=1, pady=1)
        sb = tk.Scrollbar(holder, orient="vertical", bg=PANEL_ALT,
                          troughcolor=LOG_BG, bd=0, highlightthickness=0,
                          activebackground=BORDER_LT)
        sb.pack(side="right", fill="y")
        self.log = tk.Text(holder, bg=LOG_BG, fg=TEXT, font=FONTS["log"],
                           bd=0, highlightthickness=0, wrap="none", height=9,
                           padx=12, pady=10, insertbackground=TEXT,
                           yscrollcommand=sb.set, state="disabled")
        self.log.pack(side="left", fill="both", expand=True)
        sb.configure(command=self.log.yview)
        self.log.tag_configure("time", foreground=TEXT_MUTE)
        self.log.tag_configure("INFO", foreground=GREEN)
        self.log.tag_configure("WARNING", foreground=ORANGE)
        self.log.tag_configure("ERROR", foreground=RED)
        self.log.tag_configure("CRITICAL", foreground=RED)
        self.log.tag_configure("DEBUG", foreground=TEXT_MUTE)
        self.log.tag_configure("msg", foreground=TEXT)
        self.log.tag_configure("plain", foreground=TEXT_DIM)

    def _toggle_autoscroll(self, _e=None):
        self.autoscroll = not self.autoscroll
        self.scroll_lbl.configure(
            text=("✓ 自動スクロール" if self.autoscroll else "　 自動スクロール"),
            fg=(TEXT_DIM if self.autoscroll else TEXT_MUTE))

    def _append_log(self, line: str):
        self.log.configure(state="normal")
        m = _LOGLINE.match(line)
        if m:
            t, level, msg = m.groups()
            short = {"WARNING": "WARN", "CRITICAL": "CRIT"}.get(level, level)
            self.log.insert("end", t + " ", "time")
            self.log.insert("end", f"{short:<5}", level if level in
                            ("INFO", "WARNING", "ERROR", "CRITICAL", "DEBUG") else "msg")
            self.log.insert("end", " " + msg + "\n", "msg")
        else:
            self.log.insert("end", line + "\n", "plain")
        # 際限なく溜めない
        total = int(self.log.index("end-1c").split(".")[0])
        if total > MAX_LOG_LINES:
            self.log.delete("1.0", f"{total - MAX_LOG_LINES}.0")
        self.log.configure(state="disabled")
        if self.autoscroll:
            self.log.see("end")

    # ══════════════════════════════════════════════════════════════
    # 設定の読み書き
    # ══════════════════════════════════════════════════════════════
    def _reload_configs(self, force=False):
        """設定ファイルを読み直す（更新時刻が変わったときだけ）。"""
        for path in (RUNNER_CONFIG, AUTOTRADE_CONFIG):
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if force or self._cfg_cache.get(path, (0, None))[0] != mtime:
                try:
                    self._cfg_cache[path] = (mtime, config_io.load(path))
                except Exception as e:
                    self._cfg_cache[path] = (mtime, {})
                    self._append_log(f"[コントロールパネル] {os.path.basename(path)} の読み込みに失敗: {e}")
        self.environment = self._runner_cfg().get("environment", "production")

    def _runner_cfg(self):
        return self._cfg_cache.get(RUNNER_CONFIG, (0, {}))[1] or {}

    def _autotrade_cfg(self):
        return self._cfg_cache.get(AUTOTRADE_CONFIG, (0, {}))[1] or {}

    def _strategy_value(self, path_tuple) -> bool:
        node = self._runner_cfg()
        for k in path_tuple:
            node = (node or {}).get(k) or {}
        return bool((node or {}).get("enabled"))

    def _all_strategy_values(self) -> dict:
        out = {}
        for _label, key, path in DETECTORS + AI_STRATEGIES:
            out[key] = self._strategy_value(path)
        return out

    def _at_strategy_values(self) -> dict:
        s = self._autotrade_cfg().get("strategies") or {}
        return {k: bool(s.get(k)) for _l, k in AT_STRATEGIES}

    def _at_amount_values(self) -> dict:
        cap = self._autotrade_cfg().get("capital") or {}
        return {k: cap.get(k) for _l, k in AT_AMOUNTS}

    def _snapshot_applied(self) -> dict:
        """ランナー起動時点の設定を控える（未反映バンドの判定基準）。"""
        at = self._autotrade_cfg()
        return {
            "strategies": self._all_strategy_values(),
            "symbols": config_io.file_digest(SYMBOLS_YAML),
            "at_enabled": bool(at.get("enabled")),
            "at_dry_run": bool(at.get("dry_run", True)),
            "at_strategies": self._at_strategy_values(),
            "at_amounts": self._at_amount_values(),
        }

    def _pending(self):
        """未反映の変更。(理由リスト, 未反映のストラテジーキー集合) を返す。"""
        if self.applied is None or not self.runner.is_running():
            return [], set()
        reasons, keys = [], set()
        now = self._all_strategy_values()
        for k, v in now.items():
            if self.applied["strategies"].get(k) != v:
                keys.add(k)
        if keys:
            names = {k: lbl for lbl, k, _ in DETECTORS + AI_STRATEGIES}
            on = [names[k] for k in keys if now[k]]
            off = [names[k] for k in keys if not now[k]]
            part = []
            if on:
                part.append("＋".join(on) + " ON")
            if off:
                part.append("＋".join(off) + " OFF")
            reasons.append(" / ".join(part))
        if config_io.file_digest(SYMBOLS_YAML) != self.applied["symbols"]:
            reasons.insert(0, "銘柄リスト")
        at = self._autotrade_cfg()
        if (bool(at.get("enabled")) != self.applied["at_enabled"]
                or bool(at.get("dry_run", True)) != self.applied["at_dry_run"]):
            reasons.append("自動売買 enabled/dry_run")

        # 実売買の対象戦略（キーがrunner側と重なるので "at:" を付けて区別する）
        at_now = self._at_strategy_values()
        at_names = {k: l for l, k in AT_STRATEGIES}
        at_changed = [k for k, v in at_now.items()
                      if self.applied["at_strategies"].get(k) != v]
        if at_changed:
            keys |= {f"at:{k}" for k in at_changed}
            reasons.append("実売買対象 " + "・".join(at_names[k] for k in at_changed))

        if self._at_amount_values() != self.applied["at_amounts"]:
            reasons.append("資金上限")
        return reasons, keys

    # ══════════════════════════════════════════════════════════════
    # 操作
    # ══════════════════════════════════════════════════════════════
    def _on_chip(self, chip, new_value):
        if self.busy:
            return
        label = next(l for l, k, _ in DETECTORS + AI_STRATEGIES if k == chip.key)
        try:
            config_io.set_nested_enabled(RUNNER_CONFIG, chip.key, new_value)
        except Exception as e:
            messagebox.showerror("設定の書き換えに失敗", str(e), parent=self.root)
            return
        self._reload_configs(force=True)
        self._append_log(f"[コントロールパネル] {label} を "
                         f"{'ON' if new_value else 'OFF'} にしました "
                         f"(strategies/runner/config.yaml)")
        self._refresh_ui()

    def _on_at_chip(self, chip, new_value):
        """実売買に使う戦略の切り替え（autotrade/config.yaml の strategies）。"""
        if self.busy:
            return
        label = next(l for l, k in AT_STRATEGIES if k == chip.key)
        at = self._autotrade_cfg()
        if new_value:
            # ONにする操作は「その戦略が実弾で動く」という意味になるので、
            # 今の enabled / dry_run と合わせて何が起きるかを明示して確認する
            live = at.get("enabled") and not at.get("dry_run", True)
            msg = f"「{label}」を実売買の対象にします。\n\n"
            if live:
                msg += ("現在 enabled: true / dry_run: false です。\n"
                        "★次回のランナー起動から、この戦略のシグナルで実際に発注されます★\n\n")
            elif at.get("enabled"):
                msg += "現在 dry_run: true のため、発注内容のログ出力だけです。\n\n"
            else:
                msg += "現在 enabled: false のため、まだ発注はされません。\n\n"
            msg += "対象にしますか？"
            if not messagebox.askyesno("実売買の対象に追加", msg, parent=self.root,
                                       icon="warning"):
                return
        try:
            config_io.set_child_bool(AUTOTRADE_CONFIG, "strategies", chip.key, new_value)
        except Exception as e:
            messagebox.showerror("設定の書き換えに失敗", str(e), parent=self.root)
            return
        self._reload_configs(force=True)
        self._append_log(f"[コントロールパネル] 実売買の対象: {label} を "
                         f"{'ON' if new_value else 'OFF'} にしました "
                         f"(strategies/autotrade/config.yaml)")
        self._refresh_ui()

    def save_amounts(self):
        """資金上限（capital.max_use_amount / max_amount_per_symbol）を保存する。"""
        if self.busy:
            return
        cap = (self._autotrade_cfg().get("capital") or {})
        try:
            values = {k: parse_amount(self.amount_entries[k].get())
                      for _l, k in AT_AMOUNTS}
        except ValueError as e:
            messagebox.showerror("資金上限", str(e), parent=self.root)
            return

        if any(v <= 0 for v in values.values()):
            messagebox.showerror("資金上限", "金額は1円以上で入力してください。",
                                 parent=self.root)
            return
        if values["max_amount_per_symbol"] > values["max_use_amount"]:
            messagebox.showerror(
                "資金上限",
                "1銘柄あたりの上限は、使用上限額以下にしてください。\n\n"
                f"　使用上限額　　: {values['max_use_amount']:,}円\n"
                f"　1銘柄あたり　 : {values['max_amount_per_symbol']:,}円",
                parent=self.root)
            return

        changed = {k: v for k, v in values.items() if cap.get(k) != v}
        if not changed:
            self._refresh_amount_button()
            return

        def fmt(v):
            return f"{int(v):,}円" if isinstance(v, (int, float)) else "—"

        lines = "\n".join(f"　{l}　: {fmt(cap.get(k))}  →  {values[k]:,}円"
                          for l, k in AT_AMOUNTS if k in changed)
        if not messagebox.askyesno(
                "資金上限の変更",
                "自動売買が使う金額の上限を変更します。\n"
                "1回の発注数量がこの金額から決まります。\n\n" + lines + "\n\n変更しますか？",
                parent=self.root, icon="warning"):
            return

        try:
            for k, v in changed.items():
                config_io.set_child_number(AUTOTRADE_CONFIG, "capital", k, v)
        except Exception as e:
            messagebox.showerror("設定の書き換えに失敗", str(e), parent=self.root)
            return
        self._reload_configs(force=True)
        for _l, k in AT_AMOUNTS:
            self._amount_shown.pop(k, None)      # ファイルの値で描き直させる
        self._append_log("[コントロールパネル] 資金上限を変更しました: "
                         + " / ".join(f"{l} {values[k]:,}円" for l, k in AT_AMOUNTS))
        self._refresh_ui()

    def _refresh_amount_button(self):
        """未保存の入力があるあいだ「保存」を橙にする。"""
        dirty = any(self.amount_entries[k].get().strip() != self._amount_shown.get(k, "")
                    for _l, k in AT_AMOUNTS)
        self.btn_amount.set_text("保存 *" if dirty else "保存")
        self.btn_amount.set_accent(ORANGE if dirty else None)

    def toggle_autotrade(self, key: str):
        if self.busy:
            return
        at = self._autotrade_cfg()
        cur = bool(at.get(key, True if key == "dry_run" else False))
        new = not cur

        if key == "enabled" and new:
            on = [k for k, v in (at.get("strategies") or {}).items() if v]
            dry = bool(at.get("dry_run", True))
            msg = ("自動売買を有効にします。\n\n"
                   f"　実売買の対象戦略: {', '.join(on) if on else '（なし）'}\n"
                   f"　dry_run: {'true（送信しません）' if dry else 'false ★実発注★'}\n\n")
            msg += ("対象戦略が1つも true でないため、有効にしても発注はされません。\n"
                    "strategies/autotrade/config.yaml の strategies を編集してください。\n\n"
                    if not on else
                    ("dry_run が false のため、次回のランナー起動から"
                     "実際に注文が発注されます。\n\n" if not dry else
                     "dry_run が true のため、注文内容のログ出力のみです。\n\n"))
            msg += "有効にしますか？"
            if not messagebox.askyesno("自動売買を有効化", msg, parent=self.root,
                                       icon="warning"):
                return

        if key == "dry_run" and not new:
            # dry_run を切ると本物の注文が出る。誤クリックで通り抜けないようにする
            ans = simpledialog.askstring(
                "実発注モードへの切り替え",
                "dry_run を false にすると、実際に証券口座へ注文が送信されます。\n"
                "取り消せない結果になり得ます。\n\n"
                "実行する場合は下の欄に「実発注」と入力してください。",
                parent=self.root)
            if (ans or "").strip() != "実発注":
                self._append_log("[コントロールパネル] 実発注モードへの切り替えを中止しました")
                return

        try:
            config_io.set_top_bool(AUTOTRADE_CONFIG, key, new)
        except Exception as e:
            messagebox.showerror("設定の書き換えに失敗", str(e), parent=self.root)
            return
        self._reload_configs(force=True)
        self._append_log(f"[コントロールパネル] 自動売買 {key} を {str(new).lower()} "
                         f"にしました (strategies/autotrade/config.yaml)")
        self._refresh_ui()

    def update_symbols(self):
        if self.busy:
            return
        try:
            preview = symbols_update.update(ROOT, dry_run=True)
        except Exception as e:
            messagebox.showerror("銘柄更新", f"CSVを読めませんでした。\n\n{e}", parent=self.root)
            return
        if preview["unchanged"]:
            messagebox.showinfo(
                "銘柄更新",
                f"すでに最新です。\n\n出典: {preview['source']}\n{preview['count']}銘柄",
                parent=self.root)
            return

        def fmt(codes):
            head = " ".join(codes[:12])
            return (head + f" …ほか{len(codes) - 12}件") if len(codes) > 12 else (head or "なし")

        msg = (f"出典: {preview['source']}\n"
               f"上位{preview['count']}銘柄に入れ替えます。\n\n"
               f"追加 {len(preview['added'])}件: {fmt(preview['added'])}\n"
               f"除外 {len(preview['removed'])}件: {fmt(preview['removed'])}\n\n")
        msg += ("ランナーが稼働中です。入れ替えを反映するにはランナーの再起動が必要です"
                "（更新後に「停止して再起動」ボタンが出ます）。\n\n"
                if self.runner.is_running() else "")
        msg += "更新しますか？"
        if not messagebox.askyesno("銘柄更新", msg, parent=self.root):
            return

        try:
            r = symbols_update.update(ROOT)
        except Exception as e:
            messagebox.showerror("銘柄更新", f"書き込みに失敗しました。\n\n{e}", parent=self.root)
            return
        self._append_log(f"[コントロールパネル] 監視銘柄を{r['count']}件に更新しました"
                         f"（出典 {r['source']} / 追加{len(r['added'])} 除外{len(r['removed'])}）")
        if r["backup"]:
            self._append_log(f"[コントロールパネル] 更新前のリストを "
                             f"{os.path.basename(r['backup'])} に退避しました")
        self._refresh_ui()

    # ── ランナーの起動・停止 ──
    def start_runner(self):
        if self.busy or self.runner.is_running():
            return
        if self.kabu_ok is False:
            if not messagebox.askyesno(
                    "ランナー起動",
                    f"kabuステーション（localhost:{KABU_PORTS.get(self.environment, 18080)}）に"
                    "つながりません。\n\n"
                    "kabuステーションを起動してログインし、APIを有効にしてください。\n\n"
                    "このまま起動しますか？（認証に失敗して即終了する見込みです）",
                    parent=self.root, icon="warning"):
                return
        at = self._autotrade_cfg()
        if at.get("enabled") and not at.get("dry_run", True):
            on = [k for k, v in (at.get("strategies") or {}).items() if v]
            if on and not messagebox.askyesno(
                    "実発注モードで起動します",
                    "自動売買が enabled: true / dry_run: false です。\n"
                    f"対象戦略: {', '.join(on)}\n\n"
                    "起動すると実際に注文が発注されます。よろしいですか？",
                    parent=self.root, icon="warning"):
                return
        try:
            self.runner.start()
        except Exception as e:
            messagebox.showerror("ランナー起動", str(e), parent=self.root)
            return
        self.applied = self._snapshot_applied()
        self._append_log("[コントロールパネル] ランナーを起動しました "
                         "(strategies/runner/src> python main.py --config ../config.yaml)")
        self._refresh_ui()

    def stop_runner(self):
        if self.busy or not self.runner.is_running():
            return
        held = len((self.account or {}).get("positions") or [])
        at = self._autotrade_cfg()
        if held and at.get("enabled") and not at.get("dry_run", True):
            if not messagebox.askyesno(
                    "ランナー停止",
                    f"建玉が{held}件あります。\n\n"
                    "ランナーを止めると損切り・利確・引け際の手仕舞いが行われなくなります。\n"
                    "建玉はkabuステーションから手動で管理してください。\n\n"
                    "それでも停止しますか？",
                    parent=self.root, icon="warning"):
                return
        self._run_async("停止", self._do_stop)

    def restart_runner(self):
        if self.busy:
            return
        self._run_async("再起動", self._do_restart)

    def _do_stop(self):
        code = self.runner.stop(on_progress=lambda m: self.log_queue.put(
            f"[コントロールパネル] {m}"))
        self.log_queue.put(f"[コントロールパネル] 停止しました（終了コード {code}）")
        self.applied = None

    def _do_restart(self):
        if self.runner.is_running():
            self._do_stop()
            time.sleep(1.0)     # 銘柄登録が入れ替わるので、解除が届くのを少し待つ
        # 起動はメインスレッドで行う（失敗時にダイアログを出すため）
        self.ui_queue.put(self._restart_phase2)

    def _restart_phase2(self):
        try:
            self.runner.start()
        except Exception as e:
            messagebox.showerror("ランナー起動", str(e), parent=self.root)
            return
        self.applied = self._snapshot_applied()
        self._append_log("[コントロールパネル] 新しい設定でランナーを起動しました")

    def emergency_stop(self):
        if self.busy:
            return
        at = self._autotrade_cfg()
        held = len((self.account or {}).get("positions") or [])
        msg = ("次の2つを実行します。\n\n"
               "　1. strategies/autotrade/config.yaml の enabled を false にする\n"
               "　2. ランナーを停止する\n\n")
        if held:
            msg += (f"※ 建玉が{held}件あります。建玉は自動では決済されません。\n"
                    "　 kabuステーションから手動で決済してください。\n\n")
        msg += "実行しますか？"
        if not messagebox.askyesno("緊急停止", msg, parent=self.root, icon="warning"):
            return
        try:
            if at.get("enabled"):
                config_io.set_top_bool(AUTOTRADE_CONFIG, "enabled", False)
                self._append_log("[コントロールパネル] 緊急停止: 自動売買 enabled を false にしました")
        except Exception as e:
            self._append_log(f"[コントロールパネル] 緊急停止: 設定の書き換えに失敗: {e}")
        self._reload_configs(force=True)
        if self.runner.is_running():
            self._run_async("緊急停止", self._do_stop)
        else:
            self._refresh_ui()

    def _run_async(self, label: str, fn):
        """時間のかかる操作の間、UIを触れないようにして実行する。"""
        self.busy = True
        self._refresh_ui()

        def work():
            try:
                fn()
            except Exception as e:
                self.log_queue.put(f"[コントロールパネル] {label}に失敗: {e}")
            finally:
                # ui_queue はFIFOなので、_do_restart が積んだ起動処理のあとに実行される
                self.ui_queue.put(self._done)

        threading.Thread(target=work, daemon=True, name=f"panel-{label}").start()

    def _done(self):
        self.busy = False
        self._refresh_ui()

    # ══════════════════════════════════════════════════════════════
    # 定期更新
    # ══════════════════════════════════════════════════════════════
    def _start_probe_thread(self):
        """kabuステーションの生存確認。トークンは発行しない。

        認証なしで参照系を叩き、401などのHTTP応答が返れば「APIが応答している」と判断する。
        接続拒否ならkabuステーションが起動していない（またはAPIが無効）。
        """
        def loop():
            while not self._closing:
                port = KABU_PORTS.get(self.environment, 18080)
                url = f"http://localhost:{port}/kabusapi/wallet/cash"
                try:
                    urllib.request.urlopen(url, timeout=2.0).read()
                    ok = True
                except urllib.error.HTTPError:
                    ok = True            # 401等＝APIは応答している
                except Exception:
                    ok = False
                self.kabu_ok = ok
                time.sleep(PROBE_INTERVAL)

        threading.Thread(target=loop, daemon=True, name="kabu-probe").start()

    def _read_account(self):
        try:
            mtime = os.path.getmtime(ACCOUNT_JSON)
        except OSError:
            self.account = None
            return
        # ランナーが止まったあとの古い値を出し続けないよう、鮮度を見る
        if time.time() - mtime > 120:
            self.account = None
            return
        try:
            with open(ACCOUNT_JSON, "r", encoding="utf-8") as f:
                self.account = json.load(f)
        except Exception:
            self.account = None

    def _drain(self):
        """別スレッドが積んだログと依頼を、メインスレッドで処理する（120msごと）。"""
        if self._closing:
            return
        for _ in range(300):
            try:
                self._append_log(self.log_queue.get_nowait())
            except queue.Empty:
                break
        while True:
            try:
                fn = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as e:
                self._append_log(f"[コントロールパネル] 画面更新でエラー: {e}")
        self.root.after(120, self._drain)

    def _tick(self):
        if self._closing:
            return
        self.clock.configure(text=datetime.now().strftime("%H:%M:%S"))
        self._reload_configs()
        self._read_account()
        self._refresh_ui()
        self.root.after(1000, self._tick)

    def _refresh_ui(self):
        running = self.runner.is_running()
        # 別プロセスのランナーが動いている可能性（スナップショットが更新され続けている）
        external = (not running) and self.account is not None

        # kabuステーション
        if self.kabu_ok is None:
            self.st_kabu.configure(text="● 確認中", fg=TEXT_DIM)
        elif self.kabu_ok:
            self.st_kabu.configure(text="● 接続OK", fg=GREEN)
        else:
            self.st_kabu.configure(text="● 未接続", fg=RED)

        # ランナー
        if running:
            up = int(self.runner.uptime())
            self.st_runner.configure(
                text=f"● 稼働中 {up // 3600}:{up % 3600 // 60:02d}:{up % 60:02d}", fg=GREEN)
        elif external:
            self.st_runner.configure(text="● 稼働中（別プロセス）", fg=ORANGE)
        else:
            self.st_runner.configure(text="● 停止中", fg=TEXT_DIM)
        self.hdr_dot.configure(
            fg=GREEN if running else (ORANGE if external else TEXT_MUTE))

        # 買付余力・評価損益
        acc = self.account or {}
        self.st_power.configure(text=yen(acc.get("buying_power")),
                                fg=TEXT if acc.get("buying_power") is not None else TEXT_MUTE)
        pl = acc.get("total_pl")
        rate = acc.get("total_pl_rate")
        if pl is None:
            bg, fg, dim = PANEL, TEXT_MUTE, TEXT_DIM
            text = "—"
        else:
            positive = pl >= 0
            bg = GREEN_BG if positive else RED_BG
            fg = GREEN if positive else RED
            dim = GREEN_DIM if positive else "#A06A6A"
            text = yen(pl, sign=True)
            if rate is not None:
                text += f"（{'+' if rate >= 0 else '−'}{abs(rate):.2f}%）"
        for w in (self.pl_box, self.pl_pad, self.pl_title, self.st_pl):
            w.configure(bg=bg)
        self.pl_title.configure(fg=dim)
        self.st_pl.configure(text=text, fg=fg)

        self._refresh_positions(acc)

        # 銘柄リスト
        try:
            n = len(symbols_update.current_symbols(SYMBOLS_YAML))
        except Exception:
            n = 0
        latest = symbols_update.latest_csv(ROOT)
        src = ""
        if latest:
            m = re.match(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})_", latest[0])
            src = f" · 最新CSV {m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}" if m \
                else f" · {latest[0]}"
        self.sym_label.configure(text=f"銘柄リスト {n}件{src}")

        # ストラテジーのチップ
        _reasons, pending_keys = self._pending()
        values = self._all_strategy_values()
        for key, chip in self.chips.items():
            chip.set_value(values.get(key, False), dirty=(key in pending_keys))
            chip.set_enabled(not self.busy)

        # 自動売買
        at = self._autotrade_cfg()
        at_on = bool(at.get("enabled"))
        dry = bool(at.get("dry_run", True))
        at_targets = [k for k, v in (at.get("strategies") or {}).items() if v]
        self.at_enabled_val.configure(text="ON" if at_on else "OFF",
                                      fg=RED if at_on else TEXT)
        self.at_dryrun_val.configure(text="ON" if dry else "OFF",
                                     fg=GREEN if dry else RED)
        if not at_on or not at_targets:
            self.at_badge.configure(text="停止中・安全", bg=GREEN_BG, fg=GREEN)
        elif dry:
            self.at_badge.configure(text="有効・DRY-RUN", bg="#3A2A0E", fg=ORANGE)
        else:
            self.at_badge.configure(text="★実発注★", bg=RED_BG, fg=RED)

        # 実売買に使う戦略
        at_values = self._at_strategy_values()
        for key, chip in self.at_chips.items():
            chip.set_value(at_values.get(key, False), dirty=(f"at:{key}" in pending_keys))
            chip.set_enabled(not self.busy)

        # 資金上限（入力中の欄は上書きしない）
        amounts = self._at_amount_values()
        for _l, key in AT_AMOUNTS:
            ent = self.amount_entries[key]
            ent.configure(state="normal")     # disabled のままだと書き換えられない
            v = amounts.get(key)
            text = f"{int(v):,}" if isinstance(v, (int, float)) else ""
            shown = self._amount_shown.get(key)
            if shown is None or ent.get().strip() == shown:
                if ent.get() != text:
                    ent.delete(0, "end")
                    ent.insert(0, text)
                self._amount_shown[key] = text
            if self.busy:
                ent.configure(state="disabled")
        self.btn_amount.set_enabled(not self.busy)
        self._refresh_amount_button()

        # ボタンの活殺
        self.btn_start.set_enabled(not self.busy and not running)
        self.btn_stop.set_enabled(not self.busy and running)
        self.btn_symbols.set_enabled(not self.busy)
        self.btn_panic.set_enabled(not self.busy)
        self.band_btn.set_enabled(not self.busy)
        if self.busy:
            self.btn_start.set_text("処理中…")
        else:
            self.btn_start.set_text("▶ 起動")

        self._set_band(_reasons)

    def _refresh_positions(self, acc):
        positions = acc.get("positions") or []
        if not self.account:
            self.pos_title.configure(text="建玉 —（ランナー停止中は取得できません）")
        else:
            self.pos_title.configure(text=f"建玉 {len(positions)}件")

        for w in self.pos_rows.winfo_children():
            w.destroy()
        if not positions:
            return
        for i, p in enumerate(positions):
            pl = p.get("pl")
            fg = TEXT_MUTE if pl is None else (GREEN if pl >= 0 else RED)
            cells = [
                (f"{p['symbol']} {p['name']}", "w", TEXT, FONTS["body"]),
                (f"{p['qty']:,.0f}株", "e", TEXT, FONTS["num"]),
                (f"@{p['price']:,.0f}" if p.get("price") else "—", "e", TEXT, FONTS["num"]),
                (f"→{p['current']:,.0f}" if p.get("current") else "—", "e", TEXT, FONTS["num"]),
                (yen(pl, sign=True), "e", fg, FONTS["num"]),
            ]
            for c, (txt, anchor, color, font) in enumerate(cells):
                tk.Label(self.pos_rows, text=txt, bg=PANEL, fg=color, font=font,
                         anchor=anchor).grid(row=i, column=c, sticky="ew", pady=1)

    # ══════════════════════════════════════════════════════════════
    def _on_close(self):
        if self.runner.is_running():
            if not messagebox.askyesno(
                    "終了",
                    "ランナーが稼働中です。\n\n"
                    "コントロールパネルを閉じるとランナーも停止します。\n"
                    "（ランナーはこのパネルの子プロセスとして動いているため）\n\n"
                    "終了しますか？", parent=self.root):
                return
            self.runner.stop()
        self._closing = True
        self.root.destroy()


def main():
    root = tk.Tk()
    ControlPanel(root)
    root.mainloop()


if __name__ == "__main__":
    main()
