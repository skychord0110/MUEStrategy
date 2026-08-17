# -*- coding: utf-8 -*-
"""コントロールパネルのショートカット（.lnk）をデスクトップに作る。

タスクバーには .bat を直接ピン留めできないため、pythonw.exe を直接指す
ショートカットを作り、それをピン留めしてもらう。

日本語のメッセージをここ（Python）で出すのは、cmd.exe が .bat を
OSのコードページ（日本語環境ではcp932）で読むため。UTF-8で日本語を書いた
.bat は文字化けするだけでなく、行の解析まで壊れて別のコマンドとして
実行されてしまう（実測）。

実行:
    python ui/make_shortcut.py
"""
import glob
import os
import subprocess
import sys
import winreg

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(BASE, ".."))
TARGET = os.path.join(BASE, "control_panel.pyw")
NAME = "MUEStrategy コントロールパネル.lnk"


def icon_path():
    """使うアイコン。案の記号入りのものがあればそちらを優先する。

    Windowsはアイコンの絵をパス単位でキャッシュしており、中身を差し替えても
    古い絵が出続ける。make_icon.py は案ごとに違うファイル名で書き出すので、
    そちらを指せばキャッシュに当たらず新しい絵がすぐ出る。
    """
    keyed = glob.glob(os.path.join(BASE, "control_panel_*.ico"))
    if keyed:
        return max(keyed, key=os.path.getmtime)
    return os.path.join(BASE, "control_panel.ico")


def desktop_dir() -> str:
    """デスクトップの実際の場所。OneDriveへリダイレクトされていても拾う。

    PowerShellに聞くとcp932で返ってきて日本語パスの受け取りが面倒になるので、
    レジストリから直接読む（winregはUnicodeで返す）。
    """
    try:
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            v, _ = winreg.QueryValueEx(k, "Desktop")
        path = os.path.expandvars(v)
        if os.path.isdir(path):
            return path
    except OSError:
        pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


def pythonw() -> str:
    """同じPythonの窓なし版を探す。"""
    cand = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if os.path.exists(cand):
        return cand
    from shutil import which
    return which("pythonw.exe") or sys.executable


def create_shortcut(link: str, icon: str):
    """.lnk を作る。pywin32 があればCOMで直接、無ければPowerShellに委ねる。"""
    try:
        import win32com.client
    except ImportError:
        win32com = None
    else:
        s = win32com.client.Dispatch("WScript.Shell").CreateShortCut(link)
        s.TargetPath = pythonw()
        s.Arguments = f'"{TARGET}"'
        s.WorkingDirectory = REPO
        s.IconLocation = f"{icon},0"
        s.Description = "MUEStrategy コントロールパネル"
        s.Save()
        return

    # pywin32が無い環境向けの代替。値は環境変数で渡して引用符の問題を避ける
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK);"
          "$s.TargetPath=$env:PYW;"
          "$s.Arguments='\"'+$env:TGT+'\"';"
          "$s.WorkingDirectory=$env:WD;"
          "$s.IconLocation=$env:ICO+',0';"
          "$s.Save()")
    env = dict(os.environ, LNK=link, PYW=pythonw(), TGT=TARGET, WD=REPO, ICO=icon)
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-Command", ps], env=env, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    if not os.path.exists(TARGET):
        print(f"見つかりません: {TARGET}")
        return 1
    icon = icon_path()
    if not os.path.exists(icon):
        print("アイコンを生成します…")
        subprocess.run([sys.executable, os.path.join(BASE, "make_icon.py")], check=False)
        icon = icon_path()

    link = os.path.join(desktop_dir(), NAME)
    try:
        create_shortcut(link, icon)
    except Exception as e:
        print(f"ショートカットを作れませんでした: {type(e).__name__} {e}")
        return 1
    if not os.path.exists(link):
        print(f"ショートカットが見つかりません: {link}")
        return 1

    print(f"作成しました: {link}")
    print(f"アイコン    : {os.path.basename(icon)}")
    print()
    print("タスクバーに固定する手順")
    print("  1. すでに固定してある場合は、先に右クリックして")
    print("     「タスクバーからピン留めを外す」")
    print("  2. デスクトップの「MUEStrategy コントロールパネル」を右クリック")
    print("  3. 「タスクバーにピン留めする」を選ぶ")
    print("     見当たらない場合は「その他のオプションを表示」の中にあります")
    print()
    print("※ ピン留めの操作自体はWindowsの仕様でプログラムから実行できないため、")
    print("   最後のクリックだけ手作業になります。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
