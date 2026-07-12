"""ログ出力とデスクトップのポップアップ通知を行う。"""
import logging

logger = logging.getLogger("under_surge_detector")

try:
    from plyer import notification as _plyer_notification
except ImportError:
    _plyer_notification = None


def setup_logging(log_file: str = "under_surge_detector.log"):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler()],
    )


def notify(alert: dict):
    message = (
        f"{alert['symbol']}: UNDERが{int(alert['prev_under'])}株→{int(alert['under'])}株に急増 "
        f"(+{int(alert['under_delta'])}株, +{alert['increase_pct']:.1f}%)。OVERはほぼ不変。"
        f"下値に大口買いが入った可能性（現在値{alert['price']}円・安値圏）"
    )
    logger.info("[UNDER急増] %s", message)

    if _plyer_notification is None:
        logger.warning("plyerが未インストールのためポップアップ通知はスキップします（pip install plyer）")
        return

    try:
        _plyer_notification.notify(
            title=f"UNDER急増検知 {alert['symbol']}",
            message=message,
            timeout=10,
        )
    except Exception:
        logger.exception("ポップアップ通知の送信に失敗しました")
