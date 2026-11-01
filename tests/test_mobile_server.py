# -*- coding: utf-8 -*-
"""コントロールパネルからスマホ中継サーバを立ち上げる処理（ui/mobile_server.py）のテスト。

実際にはプロセスを起動しない（Popen を差し替える）。

実行:
    python tests/test_mobile_server.py
"""
import os
import socket
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(BASE, ".."))
p = os.path.join(REPO, "ui")
if p not in sys.path:
    sys.path.insert(0, p)

import mobile_server as ms   # noqa: E402


def _fake_app(root, port):
    """隣にセットアップ済みの MUEStrategyApp があるように見せる。"""
    app = os.path.join(root, "MUEStrategyApp")
    os.makedirs(os.path.join(app, "server"))
    os.makedirs(os.path.join(app, ".venv", "Scripts"))
    open(os.path.join(app, ".venv", "Scripts", "python.exe"), "w").close()
    with open(os.path.join(app, "config.yaml"), "w", encoding="utf-8") as f:
        f.write(f"allowed_logins: [a@b]\nport: {port}\n")
    return os.path.join(root, "MUEStrategy"), app


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakePopen:
    def __init__(self):
        self.calls = []

    def __call__(self, args, **kw):
        self.calls.append((args, kw))


def test_skips_when_app_is_missing():
    with tempfile.TemporaryDirectory() as d:
        os.environ.pop("MUE_APP_DIR", None)
        fake = FakePopen()
        state, msg = ms.ensure_started(os.path.join(d, "MUEStrategy"), popen=fake)
        assert state == "skipped" and "見つかりません" in msg
        assert fake.calls == []


def test_skips_when_not_set_up():
    with tempfile.TemporaryDirectory() as d:
        os.environ.pop("MUE_APP_DIR", None)
        repo, app = _fake_app(d, _free_port())
        os.remove(os.path.join(app, "config.yaml"))
        fake = FakePopen()
        assert ms.ensure_started(repo, popen=fake)[0] == "skipped"
        assert fake.calls == []


def test_does_not_start_twice():
    with tempfile.TemporaryDirectory() as d, socket.socket() as listener:
        os.environ.pop("MUE_APP_DIR", None)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        repo, _ = _fake_app(d, listener.getsockname()[1])
        fake = FakePopen()
        assert ms.ensure_started(repo, popen=fake)[0] == "running"
        assert fake.calls == []


def test_starts_server_without_window():
    with tempfile.TemporaryDirectory() as d:
        os.environ.pop("MUE_APP_DIR", None)
        repo, app = _fake_app(d, _free_port())
        fake = FakePopen()
        state, _ = ms.ensure_started(repo, popen=fake)
        assert state == "started"
        (args, kw), = fake.calls
        assert args == [os.path.join(app, ".venv", "Scripts", "python.exe"), "-m", "server"]
        assert kw["cwd"] == app
        if os.name == "nt":
            assert kw["creationflags"] & ms.CREATE_NO_WINDOW
        assert os.path.exists(os.path.join(app, "data", "server.log"))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
