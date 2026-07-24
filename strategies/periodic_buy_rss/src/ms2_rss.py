"""マーケットスピードII RSS の RssTickList を Excel 経由で読み取るリーダー。

前提:
  - マーケットスピードII（デスクトップアプリ）が起動・ログイン済み
  - Excel に「マーケットスピードII RSS」アドインが有効化されている
  - その Excel が起動している（本リーダーは起動中の Excel にアタッチする）

仕組み:
  各銘柄について、シート上の所定セルに次の数式を書き込む:
      =RssTickList(,"<コード>.<市場>",<本数>)
  これは直近<本数>件の歩み値（時刻・出来高・約定値）をそのセルから下方向にスピル表示する。
  一定間隔でそのスピル範囲を読み取り、[(time_str, volume, price), ...] を返す。

  RssTickList はマーケットスピードII が起動している間だけ更新される。数式は起動時に一度
  書き込み、以後はポーリングで値だけ読む（毎回書き直さない）。

注意:
  - 出力項目は「時刻・出来高・約定値」の3列（売買の別は含まれない）。
  - 時刻の形式（秒まで含むか）は環境依存のため、最初の実データで確認して parse 側を調整する。
  - win32com は Windows + デスクトップExcel 専用。本モジュールは実機でのみ動作する。
    ロジック検証用に MockTickReader を用意している。
"""


class MarketSpeedTickReader:
    """起動中の Excel にアタッチし、銘柄ごとの RssTickList スピル範囲を読む。"""

    def __init__(self, symbols: list, market_suffix: str = "T", tick_count: int = 300,
                 sheet_name: str = "TICKS", anchor_row: int = 1,
                 cols_per_symbol: int = 4, newest_first: bool = True,
                 workbook_name: str = None,
                 com_retries: int = 60, com_retry_delay: float = 0.25):
        """
        symbols: 監視銘柄コードのリスト（例: ["4165", "4755"]）
        market_suffix: RssTickListに渡す市場記号（東証="T"）
        tick_count: 1銘柄あたり取得する歩み値の本数（1〜300）
        sheet_name: 数式を書き込むワークシート名
        anchor_row: 各銘柄ブロックの先頭行
        cols_per_symbol: 銘柄ブロックの列間隔（3列出力＋余白1）
        newest_first: RssTickListが新しい順に並ぶ場合True（読み取り後に古い順へ正規化）
        workbook_name: 対象ブック名。Noneならアクティブブック
        """
        self.symbols = symbols
        self.market_suffix = market_suffix
        self.tick_count = max(1, min(300, tick_count))
        self.sheet_name = sheet_name
        self.anchor_row = anchor_row
        self.cols_per_symbol = cols_per_symbol
        self.newest_first = newest_first
        self.workbook_name = workbook_name
        self.com_retries = com_retries
        self.com_retry_delay = com_retry_delay
        self._xl = None
        self._ws = None
        self._com_error = None
        # 銘柄→先頭列（1始まり）
        self._sym_col = {sym: 1 + i * cols_per_symbol for i, sym in enumerate(symbols)}

    # Excelがビジーで一時的に拒否するCOMエラー（-2147418111=RPC_E_CALL_REJECTED,
    # -2147417846=RPC_E_SERVERCALL_RETRYLATER）は少し待って再試行する。
    _BUSY_HRESULTS = (-2147418111, -2147417846)

    def _retry(self, fn, what: str = ""):
        import time as _t
        last = None
        for _ in range(self.com_retries):
            try:
                return fn()
            except self._com_error as e:
                hr = e.args[0] if e.args else None
                if hr in self._BUSY_HRESULTS:
                    last = e
                    _t.sleep(self.com_retry_delay)
                    continue
                raise
        raise RuntimeError(
            f"ExcelがCOM呼び出しを拒否し続けました（{what}）。"
            "Excelがセル編集中（Escで解除）やダイアログ表示中でないか確認してください。"
        ) from last

    def connect(self):
        """起動中のExcelにアタッチし、対象シートを用意して数式を書き込む。"""
        import win32com.client  # 実機（Windows+Excel）でのみ利用可能
        import pywintypes
        self._com_error = pywintypes.com_error

        def _attach():
            return win32com.client.GetActiveObject("Excel.Application")
        try:
            self._xl = self._retry(_attach, "Excel接続")
        except self._com_error as e:
            raise RuntimeError(
                "起動中のExcelに接続できませんでした。Excel（RSSアドイン有効）と"
                "マーケットスピードIIを起動・ログインしてから実行してください。"
            ) from e

        def _get_workbook():
            if self.workbook_name:
                for b in self._xl.Workbooks:
                    if b.Name == self.workbook_name:
                        return b
                raise RuntimeError(f"ブックが見つかりません: {self.workbook_name}")
            wb = self._xl.ActiveWorkbook
            if wb is None:
                wb = self._xl.Workbooks.Add()
            return wb
        wb = self._retry(_get_workbook, "ブック取得")

        def _get_sheet():
            for s in wb.Worksheets:
                if s.Name == self.sheet_name:
                    return s
            ws = wb.Worksheets.Add()
            ws.Name = self.sheet_name
            return ws
        self._ws = self._retry(_get_sheet, "シート用意")

        # 各銘柄ブロックの先頭セルに RssTickList を書き込む（1件ずつリトライ付き）
        for sym, col in self._sym_col.items():
            code = f"{sym}.{self.market_suffix}"
            formula = f'=RssTickList(,"{code}",{self.tick_count})'

            def _write():
                self._ws.Cells(self.anchor_row, col).Formula = formula
            self._retry(_write, f"数式書き込み({sym})")

    def read(self, symbol: str) -> list:
        """指定銘柄の歩み値を古い順の [(time_str, volume, price), ...] で返す。"""
        if self._ws is None:
            raise RuntimeError("connect() を先に呼んでください")
        col = self._sym_col[symbol]
        r1, c1 = self.anchor_row, col
        r2, c2 = self.anchor_row + self.tick_count - 1, col + 2  # 時刻・出来高・約定値の3列

        def _read():
            rng = self._ws.Range(self._ws.Cells(r1, c1), self._ws.Cells(r2, c2))
            return rng.Value  # tuple[tuple]
        values = self._retry(_read, f"読み取り({symbol})")
        return self.parse_values(values, self.newest_first)

    @staticmethod
    def parse_values(values, newest_first: bool) -> list:
        """RssTickListの読み取り値を古い順の [(time_str, volume, price)] に正規化する。

        RssTickListはヘッダー引数を省略すると先頭に見出し行 ('時刻','出来高','約定値') を出す。
        価格が数値でない行（見出し・空行）は除外する。これを残すと重複除去の基準がずれ、
        場中の新規約定を取りこぼすため必須。
        """
        rows = []
        if values:
            for row in values:
                time_str, volume, price = row[0], row[1], row[2]
                if time_str in (None, "") or price in (None, ""):
                    continue
                try:
                    float(price)  # 見出し行('約定値')や非数値行を除外
                except (TypeError, ValueError):
                    continue
                rows.append((str(time_str), volume, price))
        if newest_first:
            rows.reverse()  # 新しい順で並ぶ環境 → 古い順へ
        return rows


class MockTickReader:
    """テスト用: あらかじめ与えた銘柄別の歩み値スナップショット列を順に返す。

    snapshots[symbol] = [batch0, batch1, ...]（各batchは古い順の[(time_str, volume, price)]）
    read(symbol) を呼ぶたびに次のバッチを返す（実機のポーリングを模擬）。
    """

    def __init__(self, snapshots: dict):
        self.snapshots = {k: list(v) for k, v in snapshots.items()}
        self._idx = {k: 0 for k in snapshots}

    def connect(self):
        pass

    def read(self, symbol: str) -> list:
        seq = self.snapshots.get(symbol, [])
        i = self._idx.get(symbol, 0)
        if i >= len(seq):
            return list(seq[-1]) if seq else []
        self._idx[symbol] = i + 1
        return list(seq[i])
