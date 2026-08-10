"""統合ランナー（strategies/runner/src/main.py）の起動・停止と、出力の取り込み。

実行するコマンドは、手で叩く場合とまったく同じ:
    cd strategies/runner/src
    python main.py --config ../config.yaml

停止は次の順に試す（いきなり強制終了しない）:
    1. 停止要求ファイル state/stop.request を置く … ランナーが自分で後始末して終わる
    2. 10秒待っても終わらなければ terminate()
    3. さらに5秒待っても終わらなければ kill()

【なぜCtrl+Breakではなくファイルなのか】
Windowsでは CREATE_NO_WINDOW で起動した子プロセスは親とは別のコンソールを持つ。
コンソール制御イベント（CTRL_BREAK_EVENT）は同じコンソールに属するプロセスにしか
届かないため、この構成では停止信号が空振りして必ず強制終了に落ちてしまう
（実測: 8秒待ってから terminate される）。ファイル経由なら確実に届く。
"""
import os
import subprocess
import sys
import threading
import time

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

GRACEFUL_WAIT = 10.0
TERMINATE_WAIT = 5.0


def repo_root() -> str:
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


class RunnerProcess:
    """ランナーの子プロセス1つぶん。UIスレッドから呼ぶ前提。

    標準出力はパイプで受け取る（ランナーはファイルと標準出力の両方にログを出すため、
    パイプを読めばログファイルと同じ内容＋想定外の例外トレースまで拾える）。
    """

    def __init__(self, on_line=None, root: str = None):
        self.root = root or repo_root()
        self.src = os.path.join(self.root, "strategies", "runner", "src")
        self.stop_file = os.path.join(self.root, "strategies", "runner",
                                      "state", "stop.request")
        self.on_line = on_line          # 1行受け取るたびに呼ばれる（別スレッドから）
        self.proc = None
        self.started_at = None
        self.exit_code = None
        self._reader = None

    # ── 状態 ──
    def is_running(self) -> bool:
        if self.proc is None:
            return False
        if self.proc.poll() is None:
            return True
        if self.exit_code is None:
            self.exit_code = self.proc.returncode
        return False

    def uptime(self) -> float:
        if not self.is_running() or self.started_at is None:
            return 0.0
        return time.time() - self.started_at

    # ── 起動・停止 ──
    def start(self) -> None:
        if self.is_running():
            raise RuntimeError("すでに稼働中です")
        if not os.path.exists(os.path.join(self.src, "main.py")):
            raise FileNotFoundError(f"main.py が見つかりません: {self.src}")

        # 前回の停止要求が残っていると起動直後に止まってしまう
        try:
            os.remove(self.stop_file)
        except OSError:
            pass

        env = dict(os.environ)
        # 子の出力を即座に受け取る＋日本語を化けさせない
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        flags = 0
        if os.name == "nt":
            # コンソール窓を出さず、かつ自分のプロセスグループを持たせる。
            # 新しいグループにしておかないと、停止時のCtrl+Breakがこの
            # コントロールパネル自身にも飛んでしまう。
            flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP

        self.exit_code = None
        self.proc = subprocess.Popen(
            [sys.executable, "-u", "main.py", "--config", "../config.yaml"],
            cwd=self.src,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=flags,
        )
        self.started_at = time.time()
        self._reader = threading.Thread(target=self._pump, args=(self.proc,),
                                        daemon=True, name="runner-stdout")
        self._reader.start()

    def _pump(self, proc):
        try:
            for line in proc.stdout:
                if self.on_line:
                    self.on_line(line.rstrip("\r\n"))
        except Exception:
            pass
        finally:
            code = proc.wait()
            if self.on_line:
                if code == 0:
                    self.on_line("[コントロールパネル] ランナーが終了しました")
                else:
                    self.on_line(f"[コントロールパネル] ランナーが終了しました（終了コード {code}）")

    def stop(self, on_progress=None) -> int:
        """停止する。戻り値は終了コード（すでに停止していれば None）。"""
        if not self.is_running():
            return self.exit_code

        def say(msg):
            if on_progress:
                on_progress(msg)

        p = self.proc
        try:
            os.makedirs(os.path.dirname(self.stop_file), exist_ok=True)
            # 中身は停止したいプロセスのPID。起動処理中に要求を書いても、
            # ランナー側の「古い要求の掃除」に巻き込まれず確実に届く。
            with open(self.stop_file, "w", encoding="utf-8") as f:
                f.write(str(p.pid))
            say("停止を要求しました。ランナーの後始末を待ちます…")
        except OSError as e:
            say(f"停止要求ファイルを書けませんでした（{e}）。強制終了に切り替えます")

        try:
            return p.wait(timeout=GRACEFUL_WAIT)
        except subprocess.TimeoutExpired:
            pass

        say(f"{GRACEFUL_WAIT:.0f}秒で終わらなかったため終了要求を出します…")
        p.terminate()
        try:
            return p.wait(timeout=TERMINATE_WAIT)
        except subprocess.TimeoutExpired:
            pass

        say("応答がないため強制終了します")
        p.kill()
        return p.wait()
