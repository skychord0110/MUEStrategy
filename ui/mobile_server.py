"""スマホアプリの中継サーバ（別リポジトリ MUEStrategyApp）を立ち上げる。

コントロールパネルの起動時に呼ぶ。すでに動いていれば何もしない。
中継サーバはランナーが書き出すファイルを読むだけで、kabuステーションAPIは呼ばない
（トークンを発行するとランナーの発注が止まるため。詳細は MUEStrategyApp の CLAUDE.md）。

- 場所: このリポジトリの隣の MUEStrategyApp フォルダ（環境変数 MUE_APP_DIR で変えられる）
- 窓を出さずに起動し、出力は MUEStrategyApp/data/server.log に書く
- コントロールパネルを閉じても止めない（スマホから見続けられるように）。
  止めたいときはタスクマネージャーで MUEStrategyApp の python.exe を終了する
- MUEStrategyApp が無い・セットアップ前（.venv が無い）なら、何もせずに理由を返す
"""
import os
import socket
import subprocess

DEFAULT_PORT = 8765
LOG_MAX_BYTES = 5 * 1024 * 1024     # これを超えたら起動時に作り直す

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


def app_dir(repo_root: str) -> str:
    """MUEStrategyApp の場所。既定はこのリポジトリと同じ階層。"""
    env = os.environ.get("MUE_APP_DIR")
    if env:
        return os.path.normpath(env)
    return os.path.normpath(os.path.join(repo_root, "..", "MUEStrategyApp"))


def _port(app: str) -> int:
    """MUEStrategyApp/config.yaml の port（無ければ既定値）。"""
    try:
        import yaml
        with open(os.path.join(app, "config.yaml"), encoding="utf-8") as f:
            return int((yaml.safe_load(f) or {}).get("port") or DEFAULT_PORT)
    except Exception:
        return DEFAULT_PORT


def is_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_started(repo_root: str, popen=subprocess.Popen) -> tuple:
    """中継サーバが動いていなければ起動する。

    返り値: (状態, 説明)  状態は "running"（既に稼働中）/ "started" / "skipped" / "error"
    """
    app = app_dir(repo_root)
    python = os.path.join(app, ".venv", "Scripts", "python.exe")
    if not os.path.isdir(os.path.join(app, "server")):
        return "skipped", f"MUEStrategyApp が見つかりません（{app}）"
    if not os.path.exists(python):
        return "skipped", "MUEStrategyApp の .venv がありません（README のセットアップを先に）"
    if not os.path.exists(os.path.join(app, "config.yaml")):
        return "skipped", "MUEStrategyApp の config.yaml がありません（README のセットアップを先に）"

    port = _port(app)
    if is_listening(port):
        return "running", f"すでに稼働中です（127.0.0.1:{port}）"

    log_path = os.path.join(app, "data", "server.log")
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        if os.path.exists(log_path) and os.path.getsize(log_path) > LOG_MAX_BYTES:
            os.replace(log_path, log_path + ".old")
        log = open(log_path, "a", encoding="utf-8")
    except OSError as e:
        return "error", f"ログファイルを開けません: {e}"

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    try:
        # python.exe を窓なしで使う（pythonw.exe は標準出力が無く、ログを残せない）
        popen([python, "-m", "server"], cwd=app, env=env,
              stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
              creationflags=flags, close_fds=True)
    except OSError as e:
        return "error", f"起動できませんでした: {e}"
    finally:
        log.close()      # 子プロセスは自分の複製を持っているので、こちらは閉じてよい
    return "started", f"起動しました（127.0.0.1:{port}、ログ: {log_path}）"
