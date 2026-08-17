# -*- coding: utf-8 -*-
"""約定通知に使う音の試聴ツール。

用途ごとに候補を並べてある。気に入った番号を控えて config に設定する。

  entry  … 約定（建玉成立）: 気付きやすい音
  profit … 決済・利確      : ポップな音
  loss   … 決済・損切り    : 低めの音

実行:
    python ui/sound_preview.py set          # 新規約定→利確→損切 をセットで聴く（推奨）
    python ui/sound_preview.py set A        # セットAだけ
    python ui/sound_preview.py set 51 63 73 # 番号を指定した組み合わせ
    python ui/sound_preview.py xl_entry     # 用途で絞って再生
    python ui/sound_preview.py 53           # 番号を指定して再生
    python ui/sound_preview.py --list       # 鳴らさずに一覧だけ表示
"""
import os
import sys
import time

try:
    import winsound
except ImportError:
    winsound = None

MEDIA = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Media")

# (番号, 用途, 表示名, 種別, 指定)
#   "wav"  … Windows標準の音（ファイル名）
#   "beep" … 合成音（(周波数Hz, 長さms) の並び）。環境に一切依存しない
SOUND_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "sounds"))

CANDIDATES = [
    # ── 約定（建玉成立）: 銃声系（ui/make_sounds.py で生成した自作音） ──
    (1,  "entry", "拳銃 パン！（短く鋭い・定番）",        "gen", "shot_pistol.wav"),
    (2,  "entry", "スナップ パッ（最短・乾いた音）",       "gen", "shot_snap.wav"),
    (3,  "entry", "ライフル バァン（余韻あり）",           "gen", "shot_rifle.wav"),
    (4,  "entry", "ショットガン ドンッ（低く重い）",       "gen", "shot_shotgun.wav"),
    (5,  "entry", "サプレッサー プシュッ（控えめ）",       "gen", "shot_suppressed.wav"),
    (6,  "entry", "2連射 パパン！（強く目立つ）",          "gen", "shot_double.wav"),
    (7,  "entry", "コッキング カシャッ（金属音2連）",      "gen", "shot_cock.wav"),

    # ── 約定・少し長め（残響つき） ──
    (41, "long", "拳銃＋室内の反響（665ms）",              "gen", "long_pistol_room.wav"),
    (42, "long", "マグナム（1.0秒・鋭さと重さの両方）",     "gen", "long_magnum.wav"),
    (43, "long", "ライフル（1.2秒・遠くへ抜ける余韻）",     "gen", "long_rifle.wav"),
    (44, "long", "ショットガン（1.0秒・重く沈む）",         "gen", "long_shotgun.wav"),
    (45, "long", "屋外の山彦（1.3秒・反響がはっきり）",     "gen", "long_outdoor_echo.wav"),
    (46, "long", "2連射＋残響（1.1秒）",                   "gen", "long_double.wav"),
    (47, "long", "大砲級（1.7秒・最も重く長い）",           "gen", "long_cannon.wav"),

    # ── さらに長い版（1.7〜2.9秒）。3場面で長さを揃えてある ──
    (51, "xl_entry", "マグナム（2.0秒）",                    "gen", "xl_magnum.wav"),
    (52, "xl_entry", "ライフル（2.4秒・遠くへ抜ける）",       "gen", "xl_rifle.wav"),
    (53, "xl_entry", "大砲級（2.9秒・最も重い）",             "gen", "xl_cannon.wav"),
    (54, "xl_entry", "屋外の山彦（2.4秒・反響が続く）",       "gen", "xl_outdoor.wav"),

    (61, "xl_profit", "ポップ（1.7秒・跳ねて上がる＋きらめき）", "gen", "xl_win_pop.wav"),
    (62, "xl_profit", "ベル（2.2秒・明るい鐘のアルペジオ）",    "gen", "xl_win_bell.wav"),
    (63, "xl_profit", "ファンファーレ（2.2秒・ソドミソ）",      "gen", "xl_win_fanfare.wav"),

    (71, "xl_loss", "沈む下降音（1.8秒・低く静か）",           "gen", "xl_loss_low.wav"),
    (72, "xl_loss", "鈍い衝撃＋低い余韻（1.9秒）",             "gen", "xl_loss_thud.wav"),
    (73, "xl_loss", "やわらかい下降（2.0秒・責めない音）",      "gen", "xl_loss_soft.wav"),

    # ── 決済・利確: ポップ ──
    (12, "profit", "tada（ファンファーレ）",                              "wav", "tada.wav"),
    (13, "profit", "Windows Print complete（完了・軽快）",                "wav", "Windows Print complete.wav"),
    (14, "profit", "chimes（チャイム）",                                  "wav", "chimes.wav"),
    (15, "profit", "Windows Startup（起動音・短く明るい）",                "wav", "Windows Startup.wav"),
    (16, "profit", "Windows Logon（華やか）",                             "wav", "Windows Logon.wav"),
    (17, "profit", "Windows Proximity Connection（上がる接続音）",         "wav", "Windows Proximity Connection.wav"),
    (18, "profit", "Windows Feed Discovered（とても短く軽い）",            "wav", "Windows Feed Discovered.wav"),
    (19, "profit", "Speech On（軽い上昇音）",                             "wav", "Speech On.wav"),
    (20, "profit", "Windows Restore（ふわっと上がる）",                    "wav", "Windows Restore.wav"),
    (21, "profit", "ビープ ドミソ（上がる3音）",                           "beep", ((1047, 90), (1319, 90), (1568, 200))),
    (22, "profit", "ビープ キュピッ（高音・跳ねる2音）",                    "beep", ((1319, 70), (1976, 130))),

    # ── 決済・損切り: 低め ──
    (23, "loss", "Windows Hardware Fail（低い警告音）",                   "wav", "Windows Hardware Fail.wav"),
    (24, "loss", "chord（和音・落ち着いた失敗音）",                        "wav", "chord.wav"),
    (25, "loss", "Windows Battery Low（低く控えめ）",                     "wav", "Windows Battery Low.wav"),
    (26, "loss", "Windows Battery Critical（低く重い）",                  "wav", "Windows Battery Critical.wav"),
    (27, "loss", "Windows Logoff Sound（下がる）",                        "wav", "Windows Logoff Sound.wav"),
    (28, "loss", "Windows Shutdown（下がる・長め）",                       "wav", "Windows Shutdown.wav"),
    (29, "loss", "Windows Hardware Remove（取り外し音）",                  "wav", "Windows Hardware Remove.wav"),
    (30, "loss", "Speech Off（下降音・短い）",                            "wav", "Speech Off.wav"),
    (31, "loss", "Windows Minimize（すっと下がる・軽い）",                 "wav", "Windows Minimize.wav"),
    (32, "loss", "Windows Critical Stop（重い・強い）",                    "wav", "Windows Critical Stop.wav"),
    (33, "loss", "ビープ 下がる2音（低め）",                               "beep", ((587, 120), (392, 260))),
    (34, "loss", "ビープ 低音ひと鳴らし（ブッ）",                          "beep", ((330, 300),)),
]

GROUPS = {
    "entry":     "約定（建玉成立） — 銃声系・短め",
    "long":      "約定（建玉成立） — 銃声系・少し長め（残響つき）",
    "xl_entry":  "約定（建玉成立） — 長め（2〜3秒）",
    "xl_profit": "決済・利確 — 長め（ポップ）",
    "xl_loss":   "決済・損切り — 長め（低め）",
    "profit":    "決済・利確 — ポップな音（Windows標準・短め）",
    "loss":      "決済・損切り — 低めの音（Windows標準・短め）",
}

# 「新規約定 → 利確 → 損切」の順で聴くための組み合わせ
SETS = {
    "A": (51, 61, 71, "標準 — マグナム / ポップ / 沈む下降音"),
    "B": (53, 62, 72, "重厚 — 大砲 / ベル / 鈍い衝撃"),
    "C": (52, 63, 73, "抜け重視 — ライフル / ファンファーレ / やわらかい下降"),
    "D": (51, 63, 73, "推し — マグナム / ファンファーレ / やわらかい下降"),
}


def play(kind, spec):
    if winsound is None:
        print("    （winsoundが使えない環境です）")
        return False
    try:
        if kind in ("wav", "gen"):
            base = SOUND_DIR if kind == "gen" else MEDIA
            path = spec if os.path.isabs(spec) else os.path.join(base, spec)
            if not os.path.exists(path):
                if kind == "gen":
                    print("    先に python ui/make_sounds.py を実行してください")
                    return False
                print(f"    見つかりません: {path}")
                return False
            winsound.PlaySound(path, winsound.SND_FILENAME)   # 鳴り終わるまで待つ
        else:
            for freq, ms in spec:
                winsound.Beep(int(freq), int(ms))
        return True
    except Exception as e:
        print(f"    再生に失敗: {e}")
        return False


def _by_num(num):
    for c in CANDIDATES:
        if c[0] == num:
            return c
    return None


def play_set(nums, label=""):
    """新規約定 → 利確 → 損切 の順に鳴らす。"""
    scenes = ("新規約定", "利確", "損切り")
    if label:
        print(f"\n▼ {label}")
    for scene, num in zip(scenes, nums):
        c = _by_num(num)
        if c is None:
            print(f"  {scene:<6} {num}: 候補にありません")
            continue
        print(f"  {scene:<6} {num:>2}. {c[2]}")
        play(c[3], c[4])
        time.sleep(0.8)


def main():
    args = [a for a in sys.argv[1:]]
    listing = "--list" in args
    args = [a for a in args if a != "--list"]
    arg = args[0] if args else None

    if arg == "set":
        rest = args[1:]
        if len(rest) == 3 and all(a.isdigit() for a in rest):
            play_set([int(a) for a in rest], "指定の組み合わせ")
            return
        keys = [a.upper() for a in rest if a.upper() in SETS] or list(SETS)
        for k in keys:
            e, p, l, label = SETS[k]
            play_set((e, p, l), f"セット{k}: {label}")
            time.sleep(1.0)
        print("\n気に入ったセット記号、または3つの番号を教えてください。")
        return

    if arg and arg.isdigit():
        rows = [c for c in CANDIDATES if c[0] == int(arg)]
    elif arg in GROUPS:
        rows = [c for c in CANDIDATES if c[1] == arg]
    else:
        rows = CANDIDATES

    if not rows:
        print("該当する候補がありません")
        return

    last = None
    for num, group, name, kind, spec in rows:
        if group != last:
            print(f"\n■ {GROUPS[group]}")
            last = group
        print(f"  {num:>2}. {name}")
        if not listing:
            play(kind, spec)
            time.sleep(0.5)

    if not listing:
        print("\n気に入った番号を控えてください（約定／利確／損切りで別々に選べます）。")


if __name__ == "__main__":
    main()
