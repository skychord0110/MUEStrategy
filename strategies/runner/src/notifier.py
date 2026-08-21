"""統合ランナーのログ出力とデスクトップのポップアップ通知。

どのストラテジーの通知かをラベルで区別して1系統にまとめる。
ログは runner/logs/ ディレクトリに日付ごとのファイル（runner_YYYY-MM-DD.log）で保存する。
"""
import logging
import os
import threading
from datetime import datetime

logger = logging.getLogger("runner")

try:
    import winsound
except ImportError:      # Windows以外
    winsound = None

# ログ保存先: このファイルの場所を基準にした runner/logs/（カレントディレクトリに依存しない）
LOG_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs"))


class DailyFileHandler(logging.FileHandler):
    """日付入りファイル名（runner_YYYY-MM-DD.log）に書き、日付が変わったら自動で切り替える。"""

    def __init__(self, log_dir: str, prefix: str = "runner"):
        self.log_dir = log_dir
        self.prefix = prefix
        self.current_date = datetime.now().strftime("%Y-%m-%d")
        os.makedirs(log_dir, exist_ok=True)
        super().__init__(self._path(), encoding="utf-8")

    def _path(self) -> str:
        return os.path.join(self.log_dir, f"{self.prefix}_{self.current_date}.log")

    def emit(self, record):
        date = datetime.now().strftime("%Y-%m-%d")
        if date != self.current_date:
            # 日付が変わった: 現在のファイルを閉じ、新しい日付のファイルに切り替える
            self.current_date = date
            self.close()
            self.baseFilename = os.path.abspath(self._path())
            self.stream = None  # 次のemit時にFileHandlerが新ファイルを開く
        super().emit(record)

try:
    from plyer import notification as _plyer_notification
except ImportError:
    _plyer_notification = None

STRATEGY_LABELS = {
    "small_lot_sell_detector": "小口売り連続",
    "panic_sell_detector": "投げ売り",
    "under_surge_detector": "UNDER急増",
    "afternoon_reversal": "AI午後引け戻り",
    "afternoon_reversal_ranked": "AI午後引け戻り(順位優先)",
    "confluence": "AI複合シグナル",
    "panic_rebound": "AI投げ売り反発",
    "panic_rebound_wide": "AI投げ売り反発(幅広)",
    "periodic_buy_zscore": "定期買い集め",
    "accumulation_follow": "AI買い集め追随",
}

# AIストラテジー（仮想売買）のストラテジー名
AI_PAPER_STRATEGIES = ("afternoon_reversal", "afternoon_reversal_ranked",
                       "confluence", "panic_rebound", "panic_rebound_wide",
                       "accumulation_follow")

PANIC_STAGE_LABELS = {
    "ABSORBED": "投げ売り吸収",
    "DUMP": "買い気配へぶつけ",
}


# ── 約定音 ──────────────────────────────────────────────────────
# 鳴らすのは**実際に約定したときだけ**。検知（UNDER急増など）では鳴らさない。
# 検知は1日100件近く出るため、鳴らすと通知として機能しなくなる
# （2026-08-17: 小口売り連続58件 + UNDER急増34件）。
#
# 【なぜ音源ファイルを自前で持つのか】
# ポップアップ通知（plyer）はWindowsの古いバルーン通知APIを使っており、
# Windows 11では音が鳴らない（実測: サウンドスキームも再生デバイスも正常なのに
# 一度も鳴らなかった）。winsound.PlaySound でWAVを直接鳴らせば、
# Windowsの通知音設定に左右されずに鳴る。
# 音源は sounds/ にあり、ui/make_sounds.py で生成・再生成できる。
SOUND_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "sounds"))

DEFAULT_FILL_SOUNDS = {
    "entry":  "xl_rifle.wav",         # 新規約定: ライフル（2.4秒）
    "profit": "xl_win_fanfare.wav",   # 利確:     ファンファーレ（2.2秒）
    "loss":   "xl_loss_low.wav",      # 損切り:   沈む下降音（1.8秒）
}

# 音源が見つからないときの代替（合成音なので必ず鳴る）
FALLBACK_BEEPS = {
    "entry":  ((880, 110), (1320, 170)),
    "profit": ((1047, 90), (1319, 90), (1568, 200)),
    "loss":   ((587, 120), (392, 260)),
}

_sound = {"enabled": True, "files": dict(DEFAULT_FILL_SOUNDS), "on_dry_run": True}
_warned = set()


def configure(cfg: dict):
    """runner/config.yaml の notification セクションを反映する。"""
    cfg = cfg or {}
    _sound["enabled"] = bool(cfg.get("sound", True))
    _sound["on_dry_run"] = bool(cfg.get("sound_on_dry_run", True))
    files = cfg.get("sound_files") or {}
    for k in DEFAULT_FILL_SOUNDS:
        if files.get(k):
            _sound["files"][k] = files[k]
    if _sound["enabled"] and winsound is None:
        logger.warning("winsoundが使えない環境のため約定音は鳴りません")


def sound_enabled(dry_run: bool = False) -> bool:
    if not _sound["enabled"] or winsound is None:
        return False
    return _sound["on_dry_run"] or not dry_run


def play_fill_sound(kind: str = "entry", dry_run: bool = False):
    """約定音を鳴らす。kind は entry / profit / loss。

    再生はブロックするので必ず別スレッドで行う
    （PUSH処理や発注処理から呼ばれるため、ここで止めると板の処理が遅れる）。
    """
    if not sound_enabled(dry_run):
        return
    name = _sound["files"].get(kind) or DEFAULT_FILL_SOUNDS.get(kind)
    path = name if (name and os.path.isabs(name)) else os.path.join(SOUND_DIR, name or "")

    def run():
        try:
            if name and os.path.exists(path):
                winsound.PlaySound(path, winsound.SND_FILENAME)
                return
            if kind not in _warned:
                _warned.add(kind)
                logger.warning("音源が見つからないため代替音を鳴らします: %s"
                               "（python ui/make_sounds.py で生成できます）", path)
            for freq, ms in FALLBACK_BEEPS.get(kind, FALLBACK_BEEPS["entry"]):
                winsound.Beep(int(freq), int(ms))
        except Exception:
            logger.debug("約定音の再生に失敗しました", exc_info=True)

    threading.Thread(target=run, daemon=True, name="fill-sound").start()


# 障害を知らせる音。約定音と違い音源ファイルは持たず、winsound.Beep の合成音にする
# （音源が無い環境でも必ず鳴ってほしい種類の通知のため）。
# 「鳴らすのは約定したときだけ」という上の原則の例外。障害は1日に何度も起きるもの
# ではなく、むしろ**気づかないことが被害そのもの**だから鳴らす。
# 実例 2026-08-20: kabuステーションが落ちて6時間、誰も気づかなかった。
ALERT_BEEPS = ((1200, 150), (700, 150), (1200, 150), (700, 300))


def notify_alert(title: str, body: str):
    """障害通知。ポップアップ＋警告音で、見落とさないようにする。"""
    notify_message(title, body)
    if winsound is None:
        return

    def run():
        try:
            for freq, ms in ALERT_BEEPS:
                winsound.Beep(int(freq), int(ms))
        except Exception:
            logger.debug("警告音の再生に失敗しました", exc_info=True)

    threading.Thread(target=run, daemon=True, name="alert-sound").start()


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[DailyFileHandler(LOG_DIR), logging.StreamHandler()],
    )
    # websocket-client は切断のたびに自分でも ERROR を出す（"<原因> - goodbye"）。
    # ランナー側の on_error が同じ内容を日本語で出しているので二重になる。
    # 2026-08-20 の接続断ではこれだけで4,000行を占めたため黙らせる。
    logging.getLogger("websocket").setLevel(logging.CRITICAL)


def build_message(strategy: str, alert: dict):
    """(通知タイトル, 本文) を組み立てる。"""
    label = STRATEGY_LABELS.get(strategy, strategy)

    if strategy == "small_lot_sell_detector":
        title = f"[{label}/{alert['tier']}] {alert['symbol']}"
        body = (
            f"{alert['symbol']}: 買い気配{alert['buy_price']}円に小口売り{alert['streak']}回連続 "
            f"(直近の推定約定株数 {alert['last_volume_delta']}株)"
        )
    elif strategy == "panic_sell_detector":
        stage = PANIC_STAGE_LABELS.get(alert["stage"], alert["stage"])
        title = f"[{label}/{stage}] {alert['symbol']}"
        qty_removed = int(alert["qty_removed"])
        matched = int(alert["matched_qty"])
        if alert["stage"] == "ABSORBED":
            body = (
                f"{alert['symbol']}: OVERから消えた{qty_removed}株がほぼ同数、売り気配周辺に"
                f"指し直され、うち{matched}株が買われています（吸収進行中・現在値{alert['price']}円）"
            )
        else:  # DUMP
            body = (
                f"{alert['symbol']}: OVERから消えた{qty_removed}株とほぼ同数({matched}株)が"
                f"買い気配にぶつけられました（投げ売り・現在値{alert['price']}円）"
            )
    elif strategy == "under_surge_detector":
        title = f"[{label}] {alert['symbol']}"
        body = (
            f"{alert['symbol']}: UNDERが{int(alert['prev_under'])}株→{int(alert['under'])}株に急増 "
            f"(+{int(alert['under_delta'])}株, +{alert['increase_pct']:.1f}%)。OVERはほぼ不変。"
            f"下値に大口買いが入った可能性（現在値{alert['price']}円・安値圏）"
        )
    elif strategy in AI_PAPER_STRATEGIES:
        if alert["type"] == "ENTRY":
            title = f"[{label}/エントリー] {alert['symbol']}"
            trigger = "+".join(STRATEGY_LABELS.get(t, t) for t in alert["trigger"].split("+"))
            tp = alert.get("take_profit_pct")
            tp_txt = f"/利確+{tp:.1f}%" if tp is not None else "/利確なし"
            body = (
                f"{alert['symbol']}: {trigger} を検知、{alert['price']}円で仮想買い"
                f"（損切り-{alert['stop_loss_pct']:.1f}%{tp_txt}"
                f"/残りは大引け・発注なし）"
            )
        else:  # EXIT
            title = f"[{label}/決済:{alert['reason']}] {alert['symbol']}"
            body = (
                f"{alert['symbol']}: 仮想決済 {alert['entry_price']}円→{alert['exit_price']}円 "
                f"({alert['return_pct']:+.2f}%)"
            )
    else:
        title = f"[{label}] {alert.get('symbol', '?')}"
        body = str(alert)

    return title, body


def notify_message(title: str, body: str):
    """組み立て済みのタイトル・本文をそのまま通知する（外部ツール連携用）。"""
    logger.info("%s %s", title, body)
    if _plyer_notification is None:
        return
    try:
        _plyer_notification.notify(title=title, message=body, timeout=10)
    except Exception:
        logger.exception("ポップアップ通知の送信に失敗しました")


def notify(strategy: str, alert: dict):
    title, body = build_message(strategy, alert)
    logger.info("%s %s", title, body)

    if _plyer_notification is None:
        logger.warning("plyerが未インストールのためポップアップ通知はスキップします（pip install plyer）")
        return

    try:
        _plyer_notification.notify(title=title, message=body, timeout=10)
    except Exception:
        logger.exception("ポップアップ通知の送信に失敗しました")
