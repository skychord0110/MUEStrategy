# -*- coding: utf-8 -*-
"""「節目ライン割れ→回復」で買う日計り戦略を、節目の種類・利確目標の総当たりで検証する。

目的（利用者の要望 2026-09-21）:
  流動性の高い銘柄（liquid_under_surge と同じユニバース）に対して、
    1. 銘柄ごとの「節目のライン」を5分足で割り込む
    2. その水準を少しでも超えて回復したところで買う
    3. 価格帯別出来高やVWAPなどの節目ラインを目標に売る（日計り）
  という戦略に精度・期待値が出るかを、節目の種類と利確目標を総当たりで比べて選ぶ。

これは**読み取り専用の調査**。口座には触れない。株価は Yahoo Finance の5分足
（公開値・出来高つき）。約60日ぶんしか遡れない点は既存の weekly_report.py と同じ。

先読みの排除（weekly_report.py と同じ作法に揃える）:
  - 節目・VWAP・価格帯別出来高は、その足より**前**の足までの情報だけで作る
  - 回復は足の終値で確定させ、約定は**次の足の始値**
  - 同じ足で利確と損切りの両方に触れたら**損切りが先**に約定したとみなす（不利側に倒す）
  - 「1銘柄1日1件」の重複排除は、条件を適用した**あと**にかける

実行:
    python analysis/research_levels.py --date 2026-09-21
    python analysis/research_levels.py --date 2026-09-21 --no-fetch   # 取得済みを再利用
    python analysis/research_levels.py --date 2026-09-21 --all-symbols  # 監視50銘柄でも見る
出力:
    analysis/output/<date>/bars_5m_vol_<date>.json  取得した5分足（出来高つき）
    analysis/output/<date>/levels_trades_<date>.csv 採用した組み合わせの全トレード
    analysis/output/<date>/levels_metrics_<date>.md 総当たりの集計（これを読んで判断する）
"""
import argparse
import csv
import io
import json
import os
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from math import comb, erfc, sqrt

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(BASE, ".."))
OUTROOT = os.path.join(BASE, "output")
JST = timezone(timedelta(hours=9))

COST = 0.15              # 往復コスト（手数料＋スリッページ）の想定。weekly_report.py と揃える
MIN_PRICE = 500.0        # 既存戦略と同じ下限（低位株は値動きの質が違うため）
ENTRY_FROM = "09:05"     # 寄り直後の乱高下を避ける
ENTRY_TO = "14:30"       # 日計り。引けまでに決済する余地を残す
BREAK_VALID_BARS = 12    # 割り込みから何足以内の回復までを有効とみなすか（12足=60分）

# ── 価格帯別出来高のバケット幅（始値に対する比率）。
# 細かすぎるとPOCがノイズになり、粗いと節目として意味がなくなる。0.3%で始める。
BUCKET_PCT = 0.3
VALUE_AREA = 0.70        # バリューエリアの定義（出来高の70%）


# ── 共通の統計（weekly_report.py と同じ定義に揃える） ─────────────────
def binom_p(k, n):
    """勝ちがk回／n回。五分と違うと言えるか（両側二項検定）。

    n が大きいと厳密計算は 2**n の巨大整数になって実用時間で終わらない
    （対照群は数千件になる）。n>200 は正規近似（連続修正あり）に切り替える。
    weekly_report.py 側は n が数百までなので厳密計算のままで差は出ない。
    """
    if n == 0:
        return 1.0
    if n > 200:
        z = max(0.0, (abs(k - n / 2) - 0.5)) / sqrt(n / 4.0)
        return min(1.0, erfc(z / sqrt(2.0)))
    tail = sum(comb(n, i) for i in range(n + 1)
               if abs(i - n / 2) >= abs(k - n / 2))
    return min(1.0, tail / (2 ** n))


def stat(vals):
    """件数・勝率・平均・期待値（コスト控除後）・累計を返す。"""
    n = len(vals)
    if not n:
        return None
    w = sum(1 for v in vals if v > 0)
    mean = sum(vals) / n
    return {"n": n, "win": w / n * 100, "mean": mean, "ev": mean - COST,
            "sum": sum(vals), "star": "*" if binom_p(w, n) < 0.05 else ""}


def row(label, vals, extra=""):
    s = stat(vals)
    if not s:
        return f"| {label} | 0 | — | — | — | — |{extra}"
    return (f"| {label} | {s['n']} | {s['win']:.1f}%{s['star']} | "
            f"{s['mean']:+.2f}% | {s['ev']:+.2f}% | {s['sum']:+.1f}% |{extra}")


HEADER = ("| 区分 | 件数 | 勝率 | 平均 | 期待値 | 累計 |\n"
          "|---|---:|---:|---:|---:|---:|")


# ── 銘柄リスト ──────────────────────────────────────────────────────
def load_watch_symbols():
    import yaml
    path = os.path.join(REPO, "strategies", "symbols.yaml")
    data = yaml.safe_load(io.open(path, encoding="utf-8"))
    return [str(s["symbol"]) for s in data["symbols"]]


def load_liquid_symbols(log=print):
    """liquid_under_surge と同じユニバース（research_liquid_universe.py --write が作る）。"""
    path = os.path.join(REPO, "strategies", "runner", "state",
                        "liquid_universe.json")
    if not os.path.exists(path):
        log(f"  liquid_universe.json が無いため流動ユニバースは空: {path}")
        return [], None
    with io.open(path, encoding="utf-8") as f:
        d = json.load(f)
    return [str(s) for s in d.get("symbols", [])], d.get("date")


# ── 5分足（出来高つき）の取得 ───────────────────────────────────────
def fetch_bars(symbols, path, log=print):
    """Yahoo Financeの5分足。既存の weekly_report.fetch_bars に出来高を足したもの。

    出来高が要るのは、VWAPと価格帯別出来高（POC・バリューエリア）を作るため。
    既存の bars_5m_*.json には出来高が入っていないので別ファイルにする。
    """
    data, errors = {}, []
    for i, sym in enumerate(symbols, 1):
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}.T"
               f"?range=60d&interval=5m")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                j = json.load(resp)
            res = j["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            data[sym] = {"ts": res["timestamp"], "open": q["open"],
                         "high": q["high"], "low": q["low"], "close": q["close"],
                         "volume": q["volume"]}
        except Exception as e:
            errors.append((sym, str(e)[:60]))
        if i % 10 == 0 or i == len(symbols):
            log(f"    株価取得 {i}/{len(symbols)}")
        time.sleep(0.5)
    with open(path, "w") as f:
        json.dump(data, f)
    return data, errors


def load_bars(path):
    """symbol -> date -> [(time, o, h, l, c, v), ...] （時刻順）。"""
    with open(path) as f:
        raw = json.load(f)
    bars = defaultdict(lambda: defaultdict(list))
    for sym, d in raw.items():
        vols = d.get("volume") or [None] * len(d["ts"])
        for i, ts in enumerate(d["ts"]):
            o, h, l, c = d["open"][i], d["high"][i], d["low"][i], d["close"][i]
            if None in (o, h, l, c):
                continue
            v = vols[i] or 0
            t = datetime.fromtimestamp(ts, JST)
            bars[sym][t.strftime("%Y-%m-%d")].append((t, o, h, l, c, float(v)))
    for sym in bars:
        for day in bars[sym]:
            bars[sym][day].sort()
    return bars


# ── 節目ラインの計算（すべて「その足より前」の情報だけで作る） ────────
def round_unit(price):
    """キリ番の単位（価格帯に応じて変える版）。"""
    if price < 500:
        return 10.0
    if price < 1000:
        return 50.0
    if price < 3000:
        return 100.0
    return 500.0


def target_price(kind, entry, st, prev_close):
    """利確目標の価格。st はその足の手前までの状態。None なら目標なし（大引けまで）。"""
    if kind == "大引け":
        return None
    if kind == "VWAP":
        return st["vwap"]
    if kind == "POC":
        return st["poc"]
    if kind == "VAH(バリューエリア上限)":
        return st["vah"]
    if kind == "前日終値":
        return prev_close
    if kind == "当日高値":
        return st["day_high"]
    if kind == "キリ番上":
        u = round_unit(entry)
        return (int(entry / u) + 1) * u
    if kind.startswith("+"):
        return entry * (1 + float(kind[1:-1]) / 100.0)
    raise ValueError(kind)


def simulate(day_bars, states, i0, tgt_kind, sl_pct, prev_close):
    """i0の足の始値で買い、日計りで決済する。損切り→利確→大引けの順に判定。

    動的な目標（VWAP・POC・VAH）は各足の手前までの情報で引き直す（先読みなし）。
    """
    entry = day_bars[i0][1]
    if not entry or entry < MIN_PRICE:
        return None
    sl_px = entry * (1 - sl_pct / 100.0) if sl_pct else None
    for j in range(i0, len(day_bars)):
        _, o, h, l, c, v = day_bars[j]
        # 同じ足で両方に触れたら損切りが先（不利側に倒す）
        if sl_px is not None and l <= sl_px:
            return (sl_px / entry - 1) * 100.0
        if tgt_kind != "大引け":
            stj = states[j]
            tp = target_price(tgt_kind, entry, stj, prev_close) if stj else None
            if tp and tp > entry and h >= tp:
                return (tp / entry - 1) * 100.0
    return (day_bars[-1][4] / entry - 1) * 100.0


def build_states(day_bars, prev_close):
    """各足について「その足の手前まで」の節目・VWAP・価格帯別出来高を1回だけ作る。

    価格帯別出来高は足ごとに積み上げれば1日O(足数)で済む（毎回作り直すと
    組み合わせ総当たりで現実的な時間に終わらない）。
    """
    n = len(day_bars)
    states = [None] * n
    buckets = defaultdict(float)
    pv = vol = 0.0
    day_high = None
    base = day_bars[0][1] or day_bars[0][4]
    width = max((base or 1.0) * BUCKET_PCT / 100.0, 0.1)
    for i in range(n):
        if i > 0:
            _, o, h, l, c, v = day_bars[i - 1]
            pv += ((h + l + c) / 3.0) * v
            vol += v
            day_high = h if day_high is None else max(day_high, h)
            if v > 0:
                lo_b, hi_b = int(l / width), int(h / width)
                share = v / (hi_b - lo_b + 1)
                for b in range(lo_b, hi_b + 1):
                    buckets[b] += share
        if i == 0 or not buckets:
            continue
        vwap = (pv / vol) if vol > 0 else None
        poc, val, vah = value_area(buckets, width)
        ref = day_bars[i - 1][4]
        lows = [b[3] for b in day_bars[max(0, i - BREAK_VALID_BARS):i]]
        states[i] = {
            "levels": {
                "直近安値(60分)": min(lows) if lows else None,
                "前日終値": prev_close,
                "キリ番(適応)": (int(ref / round_unit(ref)) * round_unit(ref)) if ref else None,
                "キリ番(100円)": (int(ref / 100.0) * 100.0) if ref else None,
                "VWAP": vwap,
                "POC(価格帯別出来高)": poc,
                "VAL(バリューエリア下限)": val,
            },
            "vwap": vwap, "poc": poc, "val": val, "vah": vah,
            "day_high": day_high,
        }
    return states


def value_area(buckets, width):
    """積み上げ済みのバケットから (POC, VAL, VAH) を出す。"""
    if not buckets:
        return None, None, None
    poc_b = max(buckets, key=lambda b: buckets[b])
    total = sum(buckets.values())
    lo_b = hi_b = poc_b
    acc = buckets[poc_b]
    while acc < total * VALUE_AREA:
        up = buckets.get(hi_b + 1, 0.0)
        dn = buckets.get(lo_b - 1, 0.0)
        if up <= 0 and dn <= 0:
            break
        if up >= dn:
            hi_b += 1
            acc += up
        else:
            lo_b -= 1
            acc += dn
    center = lambda b: (b + 0.5) * width          # noqa: E731
    return center(poc_b), center(lo_b), center(hi_b)


def find_entries(day_bars, states, prev_close, level_kind):
    """「節目を割り込んだあと、その水準を少しでも超えた」最初の足を探す。

    割り込み: 足の終値が節目を下回る
    回復:     そのあとの足の終値が節目を上回る（少しでも超えれば可・利用者指定）
    約定:     回復した足の**次の足の始値**（先読みを入れない）
    """
    out = []
    broke_at = None
    broke_level = None
    for i in range(1, len(day_bars) - 1):
        st = states[i]
        if not st:
            continue
        lv = st["levels"].get(level_kind)
        if lv is None or lv <= 0:
            continue
        close = day_bars[i][4]
        if close < lv:
            broke_at, broke_level = i, lv
            continue
        if broke_at is None:
            continue
        if i - broke_at > BREAK_VALID_BARS:        # 古い割り込みは無効
            broke_at = None
            continue
        if close > broke_level:                    # 回復
            nxt = i + 1
            if nxt >= len(day_bars):
                break
            ent_t = day_bars[nxt][0]
            if not (ENTRY_FROM <= ent_t.strftime("%H:%M") <= ENTRY_TO):
                broke_at = None
                continue
            out.append({"i": nxt, "time": ent_t, "level": broke_level})
            broke_at = None
    return out


LEVEL_KINDS = ["直近安値(60分)", "前日終値", "キリ番(適応)", "キリ番(100円)",
               "VWAP", "POC(価格帯別出来高)", "VAL(バリューエリア下限)"]
TARGET_KINDS = ["大引け", "VWAP", "POC", "VAH(バリューエリア上限)", "前日終値",
                "当日高値", "キリ番上", "+1.0%", "+1.5%", "+2.0%", "+3.0%"]
SL_PCTS = [None, 1.0, 1.5, 2.0, 3.0]


def run(bars, symbols, log=print):
    """(level_kind, target_kind, sl) -> [トレード] を総当たりで作る。"""
    results = defaultdict(list)
    for sym in symbols:
        days = sorted(bars.get(sym, {}).keys())
        for di, day in enumerate(days):
            if di == 0:
                continue
            day_bars = bars[sym][day]
            if len(day_bars) < 12:
                continue
            prev_close = bars[sym][days[di - 1]][-1][4]
            states = build_states(day_bars, prev_close)
            for lk in LEVEL_KINDS:
                ents = find_entries(day_bars, states, prev_close, lk)
                if not ents:
                    continue
                for tk in TARGET_KINDS:
                    for sl in SL_PCTS:
                        picked = None
                        # 条件を適用したあとで「1銘柄1日1件」に落とす（先に間引かない）
                        for e in ents:
                            pct = simulate(day_bars, states, e["i"], tk, sl,
                                           prev_close)
                            if pct is None:
                                continue
                            picked = {"date": day, "symbol": sym,
                                      "time": e["time"].strftime("%H:%M"),
                                      "level_kind": lk, "level": e["level"],
                                      "target": tk, "sl": sl or 0,
                                      "entry": day_bars[e["i"]][1], "pct": pct}
                            break
                        if picked:
                            results[(lk, tk, sl)].append(picked)
    return results


def baseline(bars, symbols):
    """地合いの寄与を測るための対照群。

    「節目割れ→回復」に意味があるなら、同じ銘柄・同じ期間で無選別に買った場合より
    良くなければならない。検証期間が上昇基調なら買い戦略は自動的に有利に出るため、
    この差を見ないと期待値の判断ができない（過去回のレポートで繰り返し指摘されている点）。

    返り値:
      all_bars  : エントリー可能時刻の全足で買って大引け（＝日中ドリフトの平均）
      open_hold : 各日の最初の足で買って大引け（＝寄り持ち越し）
    """
    all_bars, open_hold = [], []
    for sym in symbols:
        days = sorted(bars.get(sym, {}).keys())
        for di, day in enumerate(days):
            if di == 0:
                continue
            db = bars[sym][day]
            if len(db) < 12:
                continue
            close = db[-1][4]
            first = None
            for t, o, h, l, c, v in db:
                if not (ENTRY_FROM <= t.strftime("%H:%M") <= ENTRY_TO):
                    continue
                if not o or o < MIN_PRICE:
                    continue
                all_bars.append((close / o - 1) * 100.0)
                if first is None:
                    first = o
            if first:
                open_hold.append((close / first - 1) * 100.0)
    return all_bars, open_hold


def concentration(trades):
    """銘柄の偏り（件数ではなく独立性で判断するため）。"""
    c = Counter(t["symbol"] for t in trades)
    if not c:
        return 0, 0.0
    top3 = sum(v for _, v in c.most_common(3))
    return len(c), top3 / sum(c.values()) * 100


def report(results, title, out, bars=None, symbols=None, top_n=15):
    p = lambda s="": out.append(s)                 # noqa: E731
    p(f"## {title}")
    p()

    # 0) 地合いの対照群。ここを上回らない戦略は「節目」ではなく地合いを見ているだけ
    if bars and symbols:
        ab, oh = baseline(bars, symbols)
        p("### 0. 対照群（地合いの寄与。戦略はここを上回らないと意味がない）")
        p(HEADER)
        p(row("無選別: 全足で買って大引け", ab))
        p(row("無選別: 寄りで買って大引け", oh))
        p()

    # 1) 節目の種類ごと（利確は大引け・損切りなしの素の形で比較）
    p("### 1. 節目ラインの種類（利確なし・損切りなし・大引け決済＝素の地力）")
    p(HEADER + " 銘柄数 | 上位3銘柄 |")
    base = []
    for lk in LEVEL_KINDS:
        tr = results.get((lk, "大引け", None), [])
        nsym, top3 = concentration(tr)
        p(row(lk, [t["pct"] for t in tr], f" {nsym} | {top3:.0f}% |"))
        s = stat([t["pct"] for t in tr])
        if s:
            base.append((s["ev"], s["n"], lk))
    p()

    # 2) 利確目標ごと（節目は素の地力が高かった上位3種で）
    best_levels = [lk for _, _, lk in sorted(base, reverse=True)[:3]]
    p(f"### 2. 利確目標の比較（節目は地力上位3種: {', '.join(best_levels)}・損切りなし）")
    p(HEADER)
    for tk in TARGET_KINDS:
        vals = []
        for lk in best_levels:
            vals += [t["pct"] for t in results.get((lk, tk, None), [])]
        p(row(tk, vals))
    p()

    # 3) 総当たりの上位
    p(f"### 3. 総当たり上位{top_n}（n>=20 のみ・期待値順）")
    p("| 節目 | 利確目標 | 損切り | 件数 | 勝率 | 平均 | 期待値 | 累計 | 銘柄数 | 上位3銘柄 |")
    p("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    scored = []
    for (lk, tk, sl), tr in results.items():
        s = stat([t["pct"] for t in tr])
        if s and s["n"] >= 20:
            scored.append((s["ev"], lk, tk, sl, s, tr))
    for ev, lk, tk, sl, s, tr in sorted(scored, reverse=True,
                                        key=lambda x: x[0])[:top_n]:
        nsym, top3 = concentration(tr)
        p(f"| {lk} | {tk} | {('-%.1f%%' % sl) if sl else 'なし'} | {s['n']} | "
          f"{s['win']:.1f}%{s['star']} | {s['mean']:+.2f}% | {s['ev']:+.2f}% | "
          f"{s['sum']:+.1f}% | {nsym} | {top3:.0f}% |")
    p()
    return scored


def main():
    ap = argparse.ArgumentParser(description="節目割れ→回復で買う日計り戦略の検証")
    ap.add_argument("--date", default=datetime.now(JST).strftime("%Y-%m-%d"))
    ap.add_argument("--no-fetch", action="store_true", help="株価を取得しない")
    ap.add_argument("--refetch", action="store_true", help="取得済みでも取り直す")
    ap.add_argument("--all-symbols", action="store_true",
                    help="監視50銘柄でも同じ検証をして比較する")
    a = ap.parse_args()

    outdir = os.path.join(OUTROOT, a.date)
    os.makedirs(outdir, exist_ok=True)
    bars_path = os.path.join(outdir, f"bars_5m_vol_{a.date}.json")

    liquid, uni_date = load_liquid_symbols()
    watch = load_watch_symbols()
    need = watch if a.all_symbols else liquid
    if not need:
        print("対象銘柄が0件です。research_liquid_universe.py --write を先に流してください")
        return

    if a.refetch or (not a.no_fetch and not os.path.exists(bars_path)):
        print(f"[1/3] 5分足（出来高つき）を取得（{len(need)}銘柄）…")
        _, errors = fetch_bars(need, bars_path)
        for sym, e in errors:
            print(f"    取得できず {sym}: {e}")
    else:
        print("[1/3] 取得済みの5分足を再利用")
    if not os.path.exists(bars_path):
        print("5分足がありません。--no-fetch を外して実行してください")
        return
    bars = load_bars(bars_path)

    print("[2/3] 総当たりで検証中…")
    out = []
    out.append(f"# 節目割れ→回復・日計り戦略の検証 {a.date}")
    out.append("")
    out.append("`analysis/research_levels.py` が機械的に出した数字。解釈は人が書く。")
    out.append("")
    days = sorted({d for s in bars for d in bars[s]})
    out.append(f"- 対象期間: {days[0]} 〜 {days[-1]}（{len(days)}営業日・Yahoo 5分足）")
    out.append(f"- 流動ユニバース（{uni_date}時点）: {', '.join(liquid)}")
    out.append(f"- エントリー可能時刻: {ENTRY_FROM}〜{ENTRY_TO} / {MIN_PRICE:.0f}円以上 / "
               f"割り込みから{BREAK_VALID_BARS}足以内の回復のみ / 1銘柄1日1件")
    out.append(f"- 往復コスト {COST}% を期待値から控除。`*` は二項検定 p<0.05")
    out.append("- 先読みなし: 節目・VWAP・価格帯別出来高はその足より前の情報のみ。"
               "回復は終値で確定し約定は次の足の始値。同じ足で利確と損切りに触れたら損切り優先")
    out.append("")

    liquid_res = run(bars, liquid) if liquid else {}
    scored = report(liquid_res, f"流動ユニバース（{len(liquid)}銘柄）", out,
                    bars=bars, symbols=liquid)

    if a.all_symbols:
        all_res = run(bars, watch)
        report(all_res, f"監視全銘柄（{len(watch)}銘柄・比較用）", out,
               bars=bars, symbols=watch)

    # 上位の組み合わせのトレードをCSVに落とす（後から中身を確かめられるように）
    if scored:
        best = max(scored, key=lambda x: x[0])
        trades = best[5]
        csv_path = os.path.join(outdir, f"levels_trades_{a.date}.csv")
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["date", "symbol", "time",
                                              "level_kind", "level", "target",
                                              "sl", "entry", "pct"])
            w.writeheader()
            w.writerows(trades)
        out.append(f"最良の組み合わせのトレード明細: `{os.path.basename(csv_path)}`")
        out.append("")

    md = os.path.join(outdir, f"levels_metrics_{a.date}.md")
    with io.open(md, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print(f"[3/3] 書き出しました: {md}")


if __name__ == "__main__":
    main()
