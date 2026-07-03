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
