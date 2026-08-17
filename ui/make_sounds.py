# -*- coding: utf-8 -*-
"""約定通知用の銃声系サウンドを生成して ui/sounds/ に書き出す。

Windows標準の音に銃声系が無いため合成する。外部ファイルのダウンロードや
追加ライブラリは使わない（標準ライブラリの wave / array / math / random だけ）。

銃声の作り: 白色ノイズの一気の立ち上がり＋指数減衰に、低域のサイン波（ボディ）を
重ねる。ローパスの強さで「乾いた高い音（拳銃）」から「重い低い音（ショットガン）」
まで作り分ける。

実行:
    python ui/make_sounds.py          # 生成（既存は上書き）
    python ui/make_sounds.py --play   # 生成して順に再生
"""
import array
import math
import os
import random
import sys
import wave

SR = 44100
OUT_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "sounds"))


def lowpass(samples, cutoff_hz):
    """一次ローパス。値が小さいほどこもった低い音になる。"""
    if cutoff_hz is None:
        return samples
    dt = 1.0 / SR
    rc = 1.0 / (2 * math.pi * cutoff_hz)
    a = dt / (rc + dt)
    out, prev = [], 0.0
    for x in samples:
        prev += a * (x - prev)
        out.append(prev)
    return out


def burst(dur, decay, cutoff, body_hz=0.0, body_mix=0.0, body_decay=None, seed=0):
    """ノイズの破裂音を1発つくる。

    dur       長さ（秒）
    decay     減衰の速さ（大きいほど短くキレる）
    cutoff    ローパスの遮断周波数（低いほど重い音）
    body_hz   低域のボディのの周波数（0で無し）
    body_mix  ボディの混ぜ具合（0〜1）
    """
    rnd = random.Random(seed)
    n = int(SR * dur)
    noise = [rnd.uniform(-1.0, 1.0) for _ in range(n)]
    noise = lowpass(noise, cutoff)
    bd = body_decay if body_decay is not None else decay * 0.5
    out = []
    for i in range(n):
        t = i / SR
        env = math.exp(-decay * t)
        s = noise[i] * env * (1.0 - body_mix)
        if body_hz:
            s += math.sin(2 * math.pi * body_hz * t) * math.exp(-bd * t) * body_mix
        out.append(s)
    return out


def tone(freq, dur, decay, harmonics=((1, 1.0), (2, 0.28), (3, 0.10)), delay=0.0):
    """倍音つきの音。ベルやポップな音の材料。"""
    n = int(SR * dur)
    pre = [0.0] * int(SR * delay)
    out = []
    for i in range(n):
        t = i / SR
        env = math.exp(-decay * t)
        # 立ち上がりを少しだけ丸めてプチッというノイズを防ぐ
        atk = min(1.0, t / 0.004)
        s = sum(a * math.sin(2 * math.pi * freq * h * t) for h, a in harmonics)
        out.append(s * env * atk)
    return pre + out


def glide(f0, f1, dur, decay, harmonics=((1, 1.0), (2, 0.22))):
    """周波数がなめらかに変化する音。下降させると沈む感じになる。"""
    n = int(SR * dur)
    out, phase = [], 0.0
    for i in range(n):
        t = i / SR
        f = f0 + (f1 - f0) * (t / (n / SR))
        phase += 2 * math.pi * f / SR
        env = math.exp(-decay * t) * min(1.0, t / 0.006)
        out.append(sum(a * math.sin(phase * h) for h, a in harmonics) * env)
    return out


def click(freq, dur, decay, seed=0):
    """金属質の短い音（コッキング用）。"""
    rnd = random.Random(seed)
    n = int(SR * dur)
    out = []
    for i in range(n):
        t = i / SR
        env = math.exp(-decay * t)
        s = (math.sin(2 * math.pi * freq * t) * 0.6 + rnd.uniform(-1, 1) * 0.4) * env
        out.append(s)
    return out


def silence(dur):
    return [0.0] * int(SR * dur)


def mix(*tracks):
    """長さの違う音を先頭合わせで重ねる。"""
    n = max(len(t) for t in tracks)
    out = [0.0] * n
    for t in tracks:
        for i, s in enumerate(t):
            out[i] += s
    return out


def rumble(dur, decay, cutoff, level, seed=0):
    """遠くで響く低い唸り。銃声の「余韻」を作る。"""
    rnd = random.Random(seed)
    n = int(SR * dur)
    noise = lowpass([rnd.uniform(-1.0, 1.0) for _ in range(n)], cutoff)
    return [noise[i] * math.exp(-decay * i / SR) * level for i in range(n)]


def reverb(samples, taps, tail=0.25):
    """遅延した反射音を足して残響を作る。taps は (遅延秒, 音量) の並び。"""
    max_d = max(d for d, _ in taps)
    out = [0.0] * (len(samples) + int(SR * (max_d + tail)))
    for i, s in enumerate(samples):
        out[i] += s
    for delay, gain in taps:
        off = int(SR * delay)
        for i, s in enumerate(samples):
            out[off + i] += s * gain
    return out


def normalize(samples, peak=0.85):
    m = max((abs(s) for s in samples), default=0.0)
    if m == 0:
        return samples
    g = peak / m
    return [s * g for s in samples]


def fade_out(samples, ms=8):
    n = min(int(SR * ms / 1000), len(samples))
    for i in range(n):
        samples[len(samples) - n + i] *= 1.0 - i / n
    return samples


def save(name, samples):
    os.makedirs(OUT_DIR, exist_ok=True)
    samples = fade_out(normalize(list(samples)))
    data = array.array("h", (int(max(-1.0, min(1.0, s)) * 32767) for s in samples))
    path = os.path.join(OUT_DIR, name)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data.tobytes())
    return path, len(samples) / SR


SOUNDS = {
    # 拳銃: 短く鋭い「パン！」
    "shot_pistol.wav": lambda: burst(0.18, 34, 6000, body_hz=90, body_mix=0.35, seed=1),
    # 乾いた最短音「パッ」。連続で鳴っても気にならない
    "shot_snap.wav": lambda: burst(0.09, 70, 9000, body_hz=140, body_mix=0.2, seed=2),
    # ライフル: 余韻のある「バァン」
    "shot_rifle.wav": lambda: burst(0.42, 12, 4500, body_hz=70, body_mix=0.4,
                                    body_decay=7, seed=3),
    # ショットガン: 低く重い「ドンッ」
    "shot_shotgun.wav": lambda: burst(0.35, 16, 1600, body_hz=55, body_mix=0.55,
                                      body_decay=9, seed=4),
    # サプレッサー: 控えめな「プシュッ」
    "shot_suppressed.wav": lambda: burst(0.14, 45, 3000, body_hz=110, body_mix=0.25, seed=5),
    # 2連射「パパン！」
    "shot_double.wav": lambda: (burst(0.10, 45, 6500, body_hz=95, body_mix=0.3, seed=6)
                                + silence(0.045)
                                + burst(0.20, 32, 6000, body_hz=90, body_mix=0.35, seed=7)),
    # コッキング「カシャッ」（金属2連）
    "shot_cock.wav": lambda: (click(2400, 0.05, 90, seed=8) + silence(0.05)
                              + click(1700, 0.07, 70, seed=9)),

    # ── ここから「少し長め」の銃声。残響と低い唸りを足して伸ばしている ──
    # 拳銃＋室内の反響。パン…という短い反響が付く
    "long_pistol_room.wav": lambda: reverb(
        mix(burst(0.18, 34, 6000, body_hz=90, body_mix=0.35, seed=11),
            rumble(0.35, 11, 900, 0.30, seed=12)),
        taps=[(0.035, 0.45), (0.070, 0.26), (0.115, 0.14)], tail=0.20),

    # ライフル＋長い余韻。遠くまで抜けていく感じ
    "long_rifle.wav": lambda: reverb(
        mix(burst(0.30, 15, 5000, body_hz=75, body_mix=0.40, body_decay=8, seed=13),
            rumble(0.60, 6.5, 700, 0.35, seed=14)),
        taps=[(0.060, 0.42), (0.130, 0.24), (0.220, 0.13), (0.330, 0.07)], tail=0.25),

    # ショットガン＋低い唸り。重く沈む
    "long_shotgun.wav": lambda: reverb(
        mix(burst(0.30, 14, 1500, body_hz=52, body_mix=0.55, body_decay=8, seed=15),
            rumble(0.55, 6, 350, 0.45, seed=16)),
        taps=[(0.055, 0.40), (0.120, 0.22), (0.200, 0.12)], tail=0.25),

    # マグナム。鋭さと重さの両方があり、いちばん「撃った」感が強い
    "long_magnum.wav": lambda: reverb(
        mix(burst(0.24, 20, 3800, body_hz=65, body_mix=0.48, body_decay=9, seed=17),
            rumble(0.50, 7, 550, 0.40, seed=18)),
        taps=[(0.045, 0.45), (0.095, 0.26), (0.165, 0.15), (0.250, 0.08)], tail=0.25),

    # 屋外の山彦。反響がはっきり聞こえる
    "long_outdoor_echo.wav": lambda: reverb(
        mix(burst(0.20, 26, 5500, body_hz=80, body_mix=0.38, seed=19),
            rumble(0.45, 8, 800, 0.28, seed=20)),
        taps=[(0.110, 0.42), (0.240, 0.26), (0.390, 0.16), (0.550, 0.09)], tail=0.30),

    # 2連射＋残響
    "long_double.wav": lambda: reverb(
        mix(burst(0.12, 40, 6500, body_hz=95, body_mix=0.32, seed=21)
            + silence(0.05)
            + burst(0.26, 18, 5200, body_hz=80, body_mix=0.42, body_decay=9, seed=22),
            silence(0.17) + rumble(0.45, 7, 700, 0.32, seed=23)),
        taps=[(0.050, 0.40), (0.110, 0.22), (0.190, 0.12)], tail=0.25),

    # 大砲級。最も重く長い
    "long_cannon.wav": lambda: reverb(
        mix(burst(0.40, 9, 900, body_hz=42, body_mix=0.60, body_decay=5, seed=24),
            rumble(0.85, 4, 260, 0.50, seed=25)),
        taps=[(0.080, 0.42), (0.180, 0.26), (0.300, 0.16), (0.450, 0.09)], tail=0.35),

    # ══ さらに長い版（1.5〜2.5秒）。3つの場面で長さを揃えたセット用 ══

    # ── 約定 ──
    "xl_magnum.wav": lambda: reverb(
        mix(burst(0.30, 15, 3800, body_hz=62, body_mix=0.50, body_decay=7, seed=31),
            rumble(1.10, 3.4, 520, 0.42, seed=32)),
        taps=[(0.055, 0.46), (0.120, 0.30), (0.210, 0.19), (0.330, 0.11),
              (0.480, 0.06)], tail=0.40),

    "xl_rifle.wav": lambda: reverb(
        mix(burst(0.34, 11, 5200, body_hz=72, body_mix=0.42, body_decay=6, seed=33),
            rumble(1.30, 2.8, 650, 0.40, seed=34)),
        taps=[(0.075, 0.44), (0.170, 0.29), (0.290, 0.19), (0.440, 0.12),
              (0.620, 0.06)], tail=0.45),

    "xl_cannon.wav": lambda: reverb(
        mix(burst(0.48, 7, 850, body_hz=38, body_mix=0.62, body_decay=4, seed=35),
            rumble(1.60, 2.2, 230, 0.55, seed=36)),
        taps=[(0.095, 0.44), (0.210, 0.29), (0.360, 0.19), (0.540, 0.11),
              (0.760, 0.06)], tail=0.50),

    "xl_outdoor.wav": lambda: reverb(
        mix(burst(0.24, 22, 5000, body_hz=78, body_mix=0.40, seed=37),
            rumble(1.00, 3.6, 700, 0.32, seed=38)),
        taps=[(0.130, 0.44), (0.290, 0.30), (0.480, 0.20), (0.700, 0.12),
              (0.950, 0.06)], tail=0.45),

    # ── 利確（ポップ・明るい） ──
    "xl_win_pop.wav": lambda: reverb(
        mix(tone(1319, 0.35, 9.0),                       # ミ
            tone(1976, 0.55, 6.5, delay=0.075),          # シ（跳ねる）
            tone(2637, 0.75, 5.0, delay=0.155),          # 高いミ（きらめき）
            tone(3951, 0.55, 6.0, harmonics=((1, 0.35),), delay=0.235)),
        taps=[(0.10, 0.30), (0.22, 0.18), (0.37, 0.10)], tail=0.45),

    "xl_win_bell.wav": lambda: reverb(
        mix(tone(1047, 0.70, 4.2, harmonics=((1, 1.0), (2.76, 0.30), (5.4, 0.12))),
            tone(1319, 0.75, 4.0, harmonics=((1, 0.9), (2.76, 0.26)), delay=0.10),
            tone(1568, 1.00, 3.2, harmonics=((1, 0.9), (2.76, 0.22)), delay=0.20)),
        taps=[(0.13, 0.28), (0.28, 0.17), (0.46, 0.09)], tail=0.55),

    "xl_win_fanfare.wav": lambda: reverb(
        mix(tone(784, 0.28, 10.0),                       # ソ
            tone(1047, 0.28, 10.0, delay=0.115),         # ド
            tone(1319, 0.34, 9.0, delay=0.230),          # ミ
            tone(1568, 0.95, 3.6, delay=0.345)),         # ソ（伸ばす）
        taps=[(0.12, 0.30), (0.26, 0.18), (0.43, 0.10)], tail=0.50),

    # ── 損切り（低め・責めない） ──
    "xl_loss_low.wav": lambda: reverb(
        mix(glide(233, 131, 0.90, 2.6),                  # シ♭3 → ド3 へ沈む
            rumble(1.00, 3.0, 280, 0.22, seed=41)),
        taps=[(0.10, 0.26), (0.23, 0.15), (0.39, 0.08)], tail=0.45),

    "xl_loss_thud.wav": lambda: reverb(
        mix(burst(0.22, 16, 700, body_hz=58, body_mix=0.60, body_decay=6, seed=42),
            glide(175, 98, 0.85, 3.0),
            rumble(1.10, 2.6, 230, 0.28, seed=43)),
        taps=[(0.09, 0.28), (0.20, 0.17), (0.34, 0.09)], tail=0.45),

    "xl_loss_soft.wav": lambda: reverb(
        mix(tone(392, 0.45, 5.0, harmonics=((1, 1.0), (2, 0.18))),
            tone(294, 0.90, 3.0, harmonics=((1, 1.0), (2, 0.15)), delay=0.16)),
        taps=[(0.12, 0.24), (0.26, 0.14), (0.43, 0.07)], tail=0.50),
}


def main():
    play = "--play" in sys.argv
    print(f"出力先: {OUT_DIR}\n")
    made = []
    for name, fn in SOUNDS.items():
        path, dur = save(name, fn())
        size = os.path.getsize(path) // 1024
        print(f"  {name:<22} {dur*1000:>5.0f}ms  {size:>3}KB")
        made.append(path)
    if play:
        try:
            import time
            import winsound
            print("\n再生します")
            for p in made:
                print("  ", os.path.basename(p))
                winsound.PlaySound(p, winsound.SND_FILENAME)
                time.sleep(0.4)
        except ImportError:
            print("（winsoundが無いため再生はスキップ）")


if __name__ == "__main__":
    main()
