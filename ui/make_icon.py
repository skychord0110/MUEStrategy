# -*- coding: utf-8 -*-
"""コントロールパネル用のアイコン（.ico）を生成する。

図柄は ui/icon_candidates.py の B6「円相（赤銅／墨）」。
候補を見比べて決めたものなので、図柄の定義はあちらに一本化してある。
ここで描き直すと二重管理になって食い違うため、呼び出すだけにしている。

実行:
    python ui/make_icon.py
出力:
    ui/control_panel.ico   16/24/32/48/64/128/256px を1ファイルに収めたもの

別の案に変えたいとき:
    python ui/icon_candidates.py            候補を書き出して見比べる
    python ui/icon_candidates.py --ico B3   選んだ案を control_panel.ico にする
    （そのうえで、ここの CHOICE も合わせておくと次回以降ずれない）
"""
import glob
import os
import shutil
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import icon_candidates as ic  # noqa: E402

CHOICE = "B6"


def keyed_path(key):
    """案の記号を入れたファイル名。

    Windowsはアイコンの絵を「ファイルのパス」に紐づけて覚えており、
    中身を差し替えても古い絵を出し続ける。このキャッシュはExplorerを
    止めないと消せないため、案を変えたらファイル名ごと変えて、
    キャッシュに存在しないパスにしてしまう。
    """
    return os.path.join(BASE, f"control_panel_{key.lower()}.ico")


def main():
    for key, name, fn in ic.ALL:
        if key != CHOICE:
            continue
        path = ic.write_ico(fn, keyed_path(key))
        # 名前固定のほうも同じ絵にしておく（これを指す既存のショートカット用）
        shutil.copyfile(path, ic.ICO)
        for old in glob.glob(os.path.join(BASE, "control_panel_*.ico")):
            if os.path.abspath(old) != os.path.abspath(path):
                os.remove(old)
                print(f"古い {os.path.basename(old)} を削除しました"
                      f"（指していたショートカットがあれば作り直してください）")
        sizes = ", ".join(str(s) for s in ic.ICO_SIZES)
        print(f"{key}（{name}）を書き出しました")
        print(f"  {path}")
        print(f"  {os.path.basename(ic.ICO)} にも同じものを複製")
        print(f"  {os.path.getsize(path) // 1024}KB / "
              f"{len(ic.ICO_SIZES)}サイズ: {sizes}")
        return 0
    print(f"icon_candidates.py に {CHOICE} という案がありません")
    return 1


if __name__ == "__main__":
    sys.exit(main())
