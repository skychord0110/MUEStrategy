"""仮想売買のエントリー/決済を、機械で読める形（JSON Lines）で書き出す。

スマホアプリ（別リポジトリ MUEStrategyApp）の中継サーバがこのファイルを読んで
SQLiteに取り込む。ログ（runner_YYYY-MM-DD.log）は人が読むための文言なので、
文言を直すたびにアプリ側のパーサが壊れる。ここに同じ内容を構造のまま残しておく。

出力先: strategies/runner/state/events/events_YYYY-MM-DD.jsonl （gitignore対象）
1行 = 1イベント。追記のみで、書き換えはしない。

**ここでの失敗でランナーを止めない。** 書けなかったらログに1回出して諦める。
仮想売買の記録はログにも残っているので、アプリ側はログから補える。
"""
import json
import logging
import os
import threading
from datetime import date, datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EVENTS_DIR = os.path.normpath(os.path.join(BASE_DIR, "..", "state", "events"))

# 形式を変えたら上げる。アプリ側はこの値で読み方を切り替える
SCHEMA_VERSION = 1

_lock = threading.Lock()
_warned = False


def _plain(v):
    """datetime などJSONにできない値を文字列にする。"""
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def path_for(day) -> str:
    return os.path.join(EVENTS_DIR, f"events_{day:%Y-%m-%d}.jsonl")


def record_paper(strategy: str, alert: dict, now: datetime = None) -> None:
    """AIストラテジーの ENTRY/EXIT アラートを1行追記する。"""
    global _warned
    now = now or datetime.now()
    event = {"v": SCHEMA_VERSION, "kind": "paper", "strategy": strategy,
             "logged_at": now.isoformat(timespec="milliseconds")}
    event.update({k: _plain(v) for k, v in alert.items()})
    try:
        os.makedirs(EVENTS_DIR, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False)
        with _lock:
            with open(path_for(now.date()), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:
        if not _warned:
            _warned = True
            logging.getLogger("runner").warning(
                "イベントファイルに書き込めませんでした（以降は表示しません）: %s", e)
