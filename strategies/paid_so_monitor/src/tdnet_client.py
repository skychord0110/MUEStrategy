"""TDnet非公式ミラーAPI（やのしんTDnet WEB-API, webapi.yanoshin.jp）のクライアント。

公式のTDnet APIはJPX総研との契約が必要（月額24万円〜）で個人利用には現実的でないため、
このミラーを使う。キーワード検索機能はない（日付範囲でしか絞れない）ため、
全開示を日付チャンク単位で取得し、タイトルをこちら側でフィルタする。

認証不要・APIキー不要。ただし公式サービスではないため、取りこぼし・遅延の
可能性はゼロではない（analysis/research_paid_so.py での検証時点では大きな
欠落は確認していない）。
"""
import json
import time
import urllib.request
from datetime import timedelta

TDNET_URL = "https://webapi.yanoshin.jp/webapi/tdnet/list/{rng}.json?limit=20000"

# 実測: 1回のリクエストで返せる件数に上限があり、超えると total_count=1・items=0で
# 静かに空応答になる（エラーにならない）。7日単位なら決算期の繁忙期でも安全に収まる。
SAFE_CHUNK_DAYS = 7


def fetch_range(rng, timeout=30):
    req = urllib.request.Request(TDNET_URL.format(rng=rng),
                                  headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def collect_events(start, end, require_keywords, require_title_contains,
                    exclude_words, chunk_days=SAFE_CHUNK_DAYS,
                    request_interval=0.15, log=None):
    """start〜end（date型、両端含む）の開示から条件に合うものだけ抽出する。

    require_keywords: タイトルに全て含まれていること（AND条件）
    require_title_contains: タイトルに含まれていること（例:「発行に関するお知らせ」）
    exclude_words: 1つでもタイトルに含まれていたら除外

    戻り値: [{"date", "code4", "name", "title"}, ...]（同一日・同一銘柄は1件に丸める）
    """
    events = []
    d = start
    while d <= end:
        e = min(d + timedelta(days=chunk_days - 1), end)
        rng = f"{d:%Y%m%d}-{e:%Y%m%d}"
        try:
            data = fetch_range(rng)
        except Exception as ex:
            if log:
                log.warning("TDnet取得失敗 %s: %s", rng, ex)
            d = e + timedelta(days=1)
            continue
        for it in data.get("items", []):
            t = it.get("Tdnet", {})
            title = t.get("title", "")
            if not all(k in title for k in require_keywords):
                continue
            if require_title_contains and require_title_contains not in title:
                continue
            if any(w in title for w in exclude_words):
                continue
            code = t.get("company_code", "")
            events.append({
                "date": t.get("pubdate", "")[:10],
                "code4": code[:4] if code else "",
                "name": t.get("company_name", ""),
                "title": title,
            })
        d = e + timedelta(days=1)
        time.sleep(request_interval)

    seen, out = set(), []
    for ev in events:
        k = (ev["date"], ev["code4"])
        if k in seen or not ev["code4"]:
            continue
        seen.add(k)
        out.append(ev)
    out.sort(key=lambda x: x["date"])
    return out
