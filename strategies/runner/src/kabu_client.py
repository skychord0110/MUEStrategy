"""kabuステーションAPIとの通信（認証・銘柄登録）を担当する薄いクライアント。

仕様は公式リファレンスに準拠する。
- https://kabucom.github.io/kabusapi/reference/index.html
- https://kabucom.github.io/kabusapi/ptal/push.html
"""
import requests

PORTS = {"production": 18080, "demo": 18081}


class KabuClient:
    def __init__(self, environment: str, api_password: str):
        if environment not in PORTS:
            raise ValueError(f"environment must be 'production' or 'demo', got {environment!r}")
        self.environment = environment
        self.port = PORTS[environment]
        self.base_url = f"http://localhost:{self.port}/kabusapi"
        self.ws_url = f"ws://localhost:{self.port}/kabusapi/websocket"
        self._api_password = api_password
        self.token = None

    def authenticate(self) -> str:
        resp = requests.post(f"{self.base_url}/token", json={"APIPassword": self._api_password}, timeout=10)
        if resp.status_code == 401:
            # APIが返すエラー本文（Code/Message）を添えて分かりやすく通知する
            try:
                detail = resp.json()
            except ValueError:
                detail = resp.text
            raise RuntimeError(
                f"認証に失敗しました（401）。環境={self.environment}（ポート{self.port}）用の"
                f"APIパスワードと一致していません。kabuステーションの設定→API設定で"
                f"{'本番' if self.environment == 'production' else '検証'}用パスワードを確認してください。"
                f" APIからの応答: {detail}"
            )
        resp.raise_for_status()
        data = resp.json()
        if data.get("ResultCode") != 0:
            raise RuntimeError(f"token取得に失敗しました: {data}")
        self.token = data["Token"]
        return self.token

    def _headers(self) -> dict:
        if not self.token:
            raise RuntimeError("authenticate()を先に呼んでください")
        return {"Content-Type": "application/json", "X-API-KEY": self.token}

    def unregister_all(self) -> None:
        resp = requests.put(f"{self.base_url}/unregister/all", headers=self._headers(), timeout=10)
        resp.raise_for_status()

    def register_symbols(self, symbols: list) -> dict:
        """symbols: [{"symbol": "7203", "exchange": 1}, ...]（最大50件）"""
        if len(symbols) > 50:
            raise ValueError("kabuステーションAPIのPUSH配信は最大50銘柄までです")
        body = {"Symbols": [{"Symbol": s["symbol"], "Exchange": s["exchange"]} for s in symbols]}
        resp = requests.put(f"{self.base_url}/register", json=body, headers=self._headers(), timeout=10)
        resp.raise_for_status()
        return resp.json()

    # ── 以下は参照系（GET）のみ。発注・取消は含まない ──

    def _get(self, path: str, timeout: int = 10):
        resp = requests.get(f"{self.base_url}{path}", headers=self._headers(), timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def get_wallet_cash(self) -> dict:
        """取引余力（現物）。GET /wallet/cash

        戻り値のキー:
          StockAccountWallet      現物買付可能額（合計）
          AuKCStockAccountWallet  うち、三菱UFJ eスマート証券可能額 ← 本システムはこれを使う
          AuJbnStockAccountWallet うち、auじぶん銀行残高
        """
        return self._get("/wallet/cash")

    def get_positions(self, product: str = None) -> list:
        """残高照会（保有建玉）。GET /positions

        product: 未指定=すべて / "2"=現物 / "3"=信用 （APIの product パラメータ）
        """
        path = "/positions" if product is None else f"/positions?product={product}"
        return self._get(path)

    def get_symbol(self, symbol: str, exchange: int = 1) -> dict:
        """銘柄情報。GET /symbol/{symbol}@{exchange}

        TradingUnit（売買単位）と PriceRangeGroup（呼値グループ）を得るために使う。
        日中は変わらないため起動時に取得してキャッシュする。
        """
        return self._get(f"/symbol/{symbol}@{exchange}")

    def get_board(self, symbol: str, exchange: int = 1) -> dict:
        """時価情報・板情報。GET /board/{symbol}@{exchange}

        数量計算の現在値は通常PUSHの値を使う（レート制限を避けるため）。
        本メソッドは起動直後などPUSHがまだ来ていない場合の補完用。
        """
        return self._get(f"/board/{symbol}@{exchange}")

    def get_time_and_sales(self, symbol: str, exchange: int = 1) -> dict:
        """歩み値。GET /timeandsales/{symbol}@{exchange}

        レスポンス: {"Symbol":..,"TradingPriceCount":N,
                    "TradingPrice":[{"Time":"2026-08-10T15:30:00+09:00",
                                     "Volume":..,"Price":..}, ...]}
        - 時刻はISO8601（日付・タイムゾーン付き・秒単位）
        - 直近2営業日ぶんが返る。件数の上限は実質なし
        - **銘柄登録は不要**（/board や /symbol と違いウォッチリスト外でも取れる）
        - 短時間に連続で叩くと HTTP 429（レート制限）
        """
        return self._get(f"/timeandsales/{symbol}@{exchange}", timeout=25)

    def get_orders(self, product: str = None, symbol: str = None,
                   order_id: str = None, state: str = None) -> list:
        """注文約定照会。GET /orders

        State（注文状態）: 1=待機 2=処理中 3=処理済 4=訂正取消送信中 5=終了
        （5には 発注エラー・取消済・全約定・失効・期限切れ が含まれる）
        """
        q = []
        if product:
            q.append(f"product={product}")
        if symbol:
            q.append(f"symbol={symbol}")
        if order_id:
            q.append(f"id={order_id}")
        if state:
            q.append(f"state={state}")
        path = "/orders" + ("?" + "&".join(q) if q else "")
        return self._get(path)

    # ── 以下は発注系。呼ぶと実際に注文が出る ──

    def send_order(self, payload: dict) -> dict:
        """注文発注（現物・信用）。POST /sendorder

        ⚠️ 実際に注文が発注される。呼び出し側で必ず安全弁を通すこと。
        レスポンス: {"Result": 0, "OrderId": "..."}  Result=0 が成功。
        """
        resp = requests.post(f"{self.base_url}/sendorder", json=payload,
                             headers=self._headers(), timeout=15)
        resp.raise_for_status()
        return resp.json()

    def cancel_order(self, order_id: str) -> dict:
        """注文取消。PUT /cancelorder

        ⚠️ 実際に注文が取り消される。必須は OrderId のみ。
        """
        resp = requests.put(f"{self.base_url}/cancelorder",
                            json={"OrderId": str(order_id)},
                            headers=self._headers(), timeout=15)
        resp.raise_for_status()
        return resp.json()
