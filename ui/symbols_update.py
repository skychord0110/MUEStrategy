"""extracted_stocks/ の最新CSVから strategies/symbols.yaml を作り直す。

CSVは R/Rスコアの降順で並んでいる前提で「上から N件」を採用する（既定50件）。
50件なのはPUSH配信の銘柄登録上限がアプリ全体で50銘柄のため。

単体実行:
    python ui/symbols_update.py            # 最新CSVで更新
    python ui/symbols_update.py --dry-run  # 差分だけ表示して書き込まない
"""
import argparse
import csv
import datetime as dt
import os
import re
import shutil

import yaml

MAX_SYMBOLS = 50
_STAMP = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})_export\.csv$")

HEADER = """\
# 全ストラテジー共通の監視銘柄リスト
# ここを編集すれば、全ストラテジー（small_lot_sell_detector / panic_sell_detector /
# under_surge_detector / AIStrategys / edinet_holder_monitor / periodic_buy_zscore）に反映される。
#
# 出典: extracted_stocks/{source} の上位{count}銘柄
# 最終更新: {updated}（コントロールパネルから自動生成）
#
# 注意:
#   - kabuステーションAPIのPUSH配信の銘柄登録上限は50銘柄（アプリ全体で共有）。
#   - 各ストラテジーは起動時に「全登録解除→このリストを登録」するため、
#     並走させる場合も全ストラテジーがこの同一リストを参照していれば競合しない。
#   - このファイルは起動時にしか読まれない。入れ替えたらランナーの再起動が必要。
symbols:
"""


def repo_root() -> str:
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def csv_dir(root: str = None) -> str:
    return os.path.join(root or repo_root(), "extracted_stocks")


def _sort_key(name: str, path: str):
    """ファイル名の日時で並べる。想定外の名前は更新時刻で後ろに回す。"""
    m = _STAMP.match(name)
    if m:
        return (1, dt.datetime(*(int(g) for g in m.groups())))
    try:
        return (0, dt.datetime.fromtimestamp(os.path.getmtime(path)))
    except OSError:
        return (0, dt.datetime.min)


def list_csvs(root: str = None):
    """新しい順に (ファイル名, フルパス) を返す。"""
    d = csv_dir(root)
    if not os.path.isdir(d):
        return []
    items = [(n, os.path.join(d, n)) for n in os.listdir(d) if n.lower().endswith(".csv")]
    items.sort(key=lambda t: _sort_key(*t), reverse=True)
    return items


def latest_csv(root: str = None):
    items = list_csvs(root)
    return items[0] if items else None


def read_rows(path: str, count: int = MAX_SYMBOLS):
    """CSVの上から count 件を (銘柄コード, 銘柄名) で返す。"""
    rows = []
    seen = set()
    # utf-8-sig: 先頭にBOMが付いているため（1列目のヘッダが空欄の索引列）
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "Ticker" not in reader.fieldnames:
            raise ValueError(f"Ticker列がありません: {os.path.basename(path)}")
        for r in reader:
            code = (r.get("Ticker") or "").strip()
            if not code or code in seen:
                continue
            seen.add(code)
            rows.append((code, (r.get("Name") or "").strip()))
            if len(rows) >= count:
                break
    if not rows:
        raise ValueError(f"銘柄が1件も読み取れませんでした: {os.path.basename(path)}")
    return rows


def render(rows, source: str, updated: str = None) -> str:
    updated = updated or dt.date.today().isoformat()
    out = [HEADER.format(source=source, count=len(rows), updated=updated)]
    for code, name in rows:
        out.append(f'  - symbol: "{code}"\n')
        out.append(f"    exchange: 1   # 1 = 東証  {name}\n")
    return "".join(out)


def current_symbols(path: str):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    out = []
    for s in data.get("symbols") or []:
        out.append(str(s.get("symbol")) if isinstance(s, dict) else str(s))
    return out


def update(root: str = None, count: int = MAX_SYMBOLS, dry_run: bool = False) -> dict:
    """最新CSVで symbols.yaml を作り直す。何が変わったかを辞書で返す。"""
    root = root or repo_root()
    latest = latest_csv(root)
    if not latest:
        raise FileNotFoundError("extracted_stocks/ にCSVがありません")
    name, path = latest

    rows = read_rows(path, count)
    target = os.path.join(root, "strategies", "symbols.yaml")
    before = current_symbols(target)
    after = [c for c, _ in rows]

    result = {
        "source": name,
        "path": target,
        "count": len(after),
        "added": [c for c in after if c not in before],
        "removed": [c for c in before if c not in after],
        "unchanged": before == after,
        "dry_run": dry_run,
        "backup": None,
    }
    if dry_run or before == after:
        return result

    # 書き換え前のリストは残しておく（取り違えたときに戻せるように）
    if os.path.exists(target):
        backup = target + ".bak"
        shutil.copy2(target, backup)
        result["backup"] = backup

    text = render(rows, name)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, target)
    return result


def main():
    ap = argparse.ArgumentParser(description="最新CSVで監視銘柄リストを更新する")
    ap.add_argument("--count", type=int, default=MAX_SYMBOLS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    r = update(count=args.count, dry_run=args.dry_run)
    print(f"出典: {r['source']}  → {r['count']}銘柄")
    if r["unchanged"]:
        print("変更なし（すでに最新のリストです）")
        return
    print(f"追加 {len(r['added'])}件: {' '.join(r['added']) or 'なし'}")
    print(f"除外 {len(r['removed'])}件: {' '.join(r['removed']) or 'なし'}")
    if r["dry_run"]:
        print("--dry-run のため書き込んでいません")
    else:
        print(f"書き込みました: {r['path']}")
        print("※ 反映にはランナーの再起動が必要です")


if __name__ == "__main__":
    main()
