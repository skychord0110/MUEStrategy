"""EDINET API v2 クライアント（大量保有報告書・変更報告書の取得と解析）。

APIキー必須:
  EDINET API v2 は Subscription-Key（APIキー）が必要。EDINETのサイトで無償登録して取得し、
  環境変数 EDINET_API_KEY に設定する。未設定だと 401 が返る。
  仕様書: https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/download/ESE140206.pdf

取得の流れ:
  1. 書類一覧API  GET /api/v2/documents.json?date=YYYY-MM-DD&type=2
     → その日に提出された全書類のメタデータ（docID, secCode, filerName, docTypeCode,
       docDescription 等）。ここから大量保有関連＆ウォッチリスト銘柄のものを絞り込む。
  2. 書類取得API  GET /api/v2/documents/{docID}?type=5
     → CSV(ZIP)。中のCSVは UTF-16 LE・タブ区切り。「項目名」列から
       保有株券等の数・株券等保有割合を拾う。

防御的な実装方針:
  書類種別コード（docTypeCode）やXBRLの要素名は環境・改訂で差異が出やすいため、
  コード一致だけに頼らず「書類名（docDescription）のキーワード」でも判定し、
  値の抽出も要素IDではなく日本語の「項目名」部分一致で行う。
  実データとズレる場合に備え、main.py に --debug-dump（項目名の一覧出力）を用意している。
"""
import csv
import io
import json
import os
import urllib.parse
import urllib.request
import zipfile

API_BASE = "https://api.edinet-fsa.go.jp/api/v2"
CODELIST_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"

# 大量保有報告書まわりの書類種別コード（実データで確認: 350=大量保有報告書・変更報告書、360=訂正）
DEFAULT_DOC_TYPE_CODES = ("350", "360")
DEFAULT_KEYWORDS = ("大量保有", "変更報告書")


class EdinetCodeMap:
    """EDINETコード → 証券コード(4桁) の対応表。

    大量保有報告書の「提出者」は保有者（ファンド・個人）であり、secCode は基本 null。
    銘柄（発行会社）は issuerEdinetCode に入るため、この対応表で証券コードへ変換する。
    EDINETが公開するコードリスト（CSV/ZIP・cp932）を取得してキャッシュする。
    """

    def __init__(self, cache_path: str = None, max_age_days: int = 7):
        self.cache_path = cache_path
        self.max_age_days = max_age_days
        self.map = {}

    def load(self) -> dict:
        import time as _t
        if self.cache_path and os.path.exists(self.cache_path):
            age_days = (_t.time() - os.path.getmtime(self.cache_path)) / 86400
            if age_days <= self.max_age_days:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    self.map = json.load(f)
                return self.map
        req = urllib.request.Request(CODELIST_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
            text = zf.read(name).decode("cp932", errors="replace")
        # 1行目はダウンロード実行日の情報。2行目がヘッダー。
        lines = text.splitlines()
        m = {}
        for r in csv.DictReader(io.StringIO("\n".join(lines[1:]))):
            ec = (r.get("ＥＤＩＮＥＴコード") or "").strip()
            sec = (r.get("証券コード") or "").strip()
            if ec and sec:
                m[ec] = sec[:4]   # 証券コードは5桁表記（例: "48130"）なので先頭4桁
        self.map = m
        if self.cache_path:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(m, f)
        return m

    def to_symbol(self, doc: dict) -> str:
        """書類メタデータから対象銘柄の証券コード(4桁)を割り出す。"""
        issuer = (doc.get("issuerEdinetCode") or "").strip()
        if issuer and issuer in self.map:
            return self.map[issuer]
        subject = (doc.get("subjectEdinetCode") or "").strip()
        if subject and subject in self.map:
            return self.map[subject]
        sec = (doc.get("secCode") or "").strip()
        return sec[:4] if sec else None


class EdinetClient:
    def __init__(self, api_key: str = None, timeout: int = 30):
        self.api_key = api_key or os.environ.get("EDINET_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "EDINET APIキーが未設定です。EDINETでAPIキーを取得し、環境変数 "
                "EDINET_API_KEY に設定してください（例: $env:EDINET_API_KEY = \"...\"）。"
            )
        self.timeout = timeout

    def _get(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.read()

    def list_documents(self, date_str: str) -> list:
        """指定日に提出された書類のメタデータ一覧を返す（type=2）。"""
        q = urllib.parse.urlencode({"date": date_str, "type": 2,
                                    "Subscription-Key": self.api_key})
        raw = self._get(f"{API_BASE}/documents.json?{q}")
        data = json.loads(raw.decode("utf-8"))
        status = str((data.get("metadata") or {}).get("status", ""))
        if status and status not in ("200",):
            msg = (data.get("metadata") or {}).get("message", "")
            raise RuntimeError(f"EDINET書類一覧の取得に失敗しました (status={status}): {msg}")
        return data.get("results") or []

    @staticmethod
    def filter_large_holding(results: list, symbols: set, code_map,
                             doc_type_codes=DEFAULT_DOC_TYPE_CODES,
                             keywords=DEFAULT_KEYWORDS) -> list:
        """大量保有関連 かつ ウォッチリスト銘柄 の書類だけ抜き出す。

        大量保有報告書の提出者は保有者（ファンド・個人）で secCode は基本 null のため、
        issuerEdinetCode を EdinetCodeMap で証券コードに変換して突き合わせる。
        """
        picked = []
        for r in results:
            dtc = str(r.get("docTypeCode") or "")
            desc = r.get("docDescription") or ""
            if dtc not in doc_type_codes and not any(k in desc for k in keywords):
                continue
            code4 = code_map.to_symbol(r)
            if code4 and code4 in symbols:
                picked.append({**r, "symbol": code4})
        return picked

    def fetch_document_csv(self, doc_id: str) -> list:
        """書類のCSV(ZIP)を取得し、[{列名: 値}, ...] のリストで返す。

        EDINETのCSVは UTF-16 LE・タブ区切り。ZIP内に複数CSVが入ることがあるため全て連結する。
        """
        q = urllib.parse.urlencode({"type": 5, "Subscription-Key": self.api_key})
        raw = self._get(f"{API_BASE}/documents/{doc_id}?{q}")
        rows = []
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for name in zf.namelist():
                if not name.lower().endswith(".csv"):
                    continue
                blob = zf.read(name)
                text = None
                for enc in ("utf-16", "utf-16-le", "cp932", "utf-8-sig"):
                    try:
                        text = blob.decode(enc)
                        break
                    except (UnicodeDecodeError, UnicodeError):
                        continue
                if text is None:
                    continue
                rows.extend(list(csv.DictReader(io.StringIO(text), delimiter="\t")))
        return rows


def _to_number(s):
    if s is None:
        return None
    t = str(s).strip().replace(",", "").replace("株", "").replace("%", "")
    if t in ("", "-", "－", "×"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


# 実データ（jplvh_cor タクソノミ）で確認済みの要素ID。
#   保有割合は小数で入る（例: 0.1330 = 13.30%）ため、100倍して%に直す。
ELEM_SHARES = "jplvh_cor:TotalNumberOfStocksEtcHeld"            # 保有株券等の数（総数）
ELEM_OUTSTANDING = "jplvh_cor:TotalNumberOfOutstandingStocksEtc"  # 発行済株式等総数
ELEM_RATIO = "jplvh_cor:HoldingRatioOfShareCertificatesEtc"     # 株券等保有割合
ELEM_RATIO_PREV = "jplvh_cor:HoldingRatioOfShareCertificatesEtcPerLastReport"
ELEM_DATE = "jplvh_cor:DateWhenFilingRequirementAroseCoverPage"  # 報告義務発生日
ELEM_NAME = "jplvh_cor:Name"                                     # 氏名又は名称
ELEM_PURPOSE = "jplvh_cor:PurposeOfHolding"                      # 保有目的


def extract_holding(rows: list) -> dict:
    """CSV行から保有株数・保有割合・報告義務発生日・提出者名を抜き出す。

    要素IDでの一致を優先し、タクソノミ改訂に備えて日本語の項目名でもフォールバックする。
    同じ項目が複数回出る場合は最後に出た値を採用する。
    """
    out = {"shares": None, "ratio": None, "ratio_prev": None,
           "outstanding": None, "report_date": None, "filer": None, "purpose": None}
    for r in rows:
        eid = (r.get("要素ID") or "").strip()
        name = (r.get("項目名") or "").strip()
        value = r.get("値")
        num = _to_number(value)

        if eid == ELEM_SHARES or (eid == "" and "保有株券等の数（総数）" in name):
            if num is not None:
                out["shares"] = num
        elif eid == ELEM_OUTSTANDING:
            if num is not None:
                out["outstanding"] = num
        elif eid == ELEM_RATIO:
            if num is not None:
                out["ratio"] = num * 100.0   # 小数 → %
        elif eid == ELEM_RATIO_PREV:
            if num is not None:
                out["ratio_prev"] = num * 100.0
        elif eid == ELEM_DATE:
            if value and str(value).strip() not in ("－", "-", ""):
                out["report_date"] = str(value).strip()
        elif eid == ELEM_NAME:
            if value and str(value).strip() not in ("－", "-", ""):
                out["filer"] = str(value).strip()
        elif eid == ELEM_PURPOSE:
            if value:
                out["purpose"] = str(value).strip()

    # 要素IDが取れなかった場合の保険（項目名ベース）
    if out["shares"] is None or out["ratio"] is None:
        for r in rows:
            name = (r.get("項目名") or "").strip()
            num = _to_number(r.get("値"))
            if num is None:
                continue
            if out["shares"] is None and "保有株券等の数" in name:
                out["shares"] = num
            elif out["ratio"] is None and "株券等保有割合" in name and "直前" not in name:
                # 割合は小数表記（1未満）なら%へ換算
                out["ratio"] = num * 100.0 if num <= 1.0 else num
    return out


def dump_item_names(rows: list, limit: int = 80) -> list:
    """デバッグ用: CSVに含まれる項目名と値を一覧化する（実データとの突き合わせ用）。"""
    out = []
    for r in rows[:limit]:
        out.append({"要素ID": r.get("要素ID"), "項目名": r.get("項目名"), "値": r.get("値")})
    return out
