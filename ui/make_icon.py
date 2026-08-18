# -*- coding: utf-8 -*-
"""コントロールパネル用のアイコン（.ico）を生成する。

図柄の定義も切り替えの処理も ui/icon_candidates.py にある。
ここはいま選んでいる案（CHOICE）を指定して呼ぶだけ。

実行:
    python ui/make_icon.py          いまの CHOICE で作り直す

別の案に変えたいとき（下の CHOICE も自動で書き換わる）:
    python ui/icon_candidates.py            候補を書き出して見比べる
    python ui/icon_candidates.py --ico N2   選んだ案に切り替える
そのあと必ずショートカットを作り直すこと:
    ショートカットを作る.bat
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import icon_candidates as ic  # noqa: E402

CHOICE = "R10"


def main():
    try:
        path, name, removed = ic.apply_icon(CHOICE)
    except KeyError:
        print(f"icon_candidates.py に {CHOICE} という案がありません")
        print(f"  選べるのは {', '.join(k for k, _, _ in ic.ALL)}")
        return 1
    sizes = ", ".join(str(s) for s in ic.ICO_SIZES)
    print(f"{CHOICE}（{name}）を書き出しました")
    print(f"  {path}")
    print(f"  {os.path.basename(ic.ICO)} にも同じものを複製")
    print(f"  {os.path.getsize(path) // 1024}KB / "
          f"{len(ic.ICO_SIZES)}サイズ: {sizes}")
    for r in removed:
        print(f"  前の {r} を削除（指していたショートカットは作り直しが要ります）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
