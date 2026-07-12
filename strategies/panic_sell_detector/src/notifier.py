"""ログ出力とデスクトップのポップアップ通知を行う。"""
import logging

logger = logging.getLogger("panic_sell_detector")

try:
    from plyer import notification as _plyer_notification
except ImportError:
    _plyer_notification = None

STAGE_LABELS = {
    "ABSORBED": "投げ売り吸収",
    "DUMP": "買い気配へぶつけ",
}


def setup_logging(log_file: str = "panic_sell_detector.log"):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler()],
    )


def notify(alert: dict):
    stage = alert["stage"]
    qty_removed = int(alert["qty_removed"])
    matched = int(alert["matched_qty"])

    if stage == "ABSORBED":
        message = (
            f"{alert['symbol']}: OVERから消えた{qty_removed}株がほぼ同数、売り気配周辺に"
            f"指し直され、うち{matched}株が買われています（吸収進行中・現在値{alert['price']}円）"
        )
    else:  # DUMP
        message = (
            f"{alert['symbol']}: OVERから消えた{qty_removed}株とほぼ同数({matched}株)が"
            f"買い気配にぶつけられました（投げ売り・現在値{alert['price']}円）"
        )

    logger.info("[%s] %s", STAGE_LABELS.get(stage, stage), message)

    if _plyer_notification is None:
        logger.warning("plyerが未インストールのためポップアップ通知はスキップします（pip install plyer）")
        return

    try:
        _plyer_notification.notify(
            title=f"投げ売り検知 [{STAGE_LABELS.get(stage, stage)}] {alert['symbol']}",
            message=message,
            timeout=10,
        )
    except Exception:
        logger.exception("ポップアップ通知の送信に失敗しました")
