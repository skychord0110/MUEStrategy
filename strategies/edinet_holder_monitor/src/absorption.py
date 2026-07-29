"""大口保有者の残存保有株が市場で「こなされた」量を推定するモデル（純Python・API非依存）。

考え方:
  大量保有報告書（変更報告書）には、報告義務発生日時点の「保有株券等の数」が記載される。
  その後、保有者が売却を続けている間は、平常時より出来高が膨らむ。
  そこで「平常出来高を超えた分（超過出来高）＝保有者の売り玉が市場で消化された量」と仮定し、
  報告日以降の超過出来高を積み上げて、残存保有株がゼロになるタイミングを推定する。

  消化量 = Σ (その日の出来高 − 平常出来高) × 重み

  重みは下落日/上昇日で変えられる（既定は両方1.0＝区別しない）。下落局面でこなされている
  という見立てを強めたい場合は up_day_weight を下げる。
  seller_share は「超過出来高のうち当該保有者に帰属する割合」（既定1.0＝全部）。
  1.0 は消化を早く見積もるため、慎重に見るなら 0.5〜0.8 に下げる。

限界:
  超過出来高が本当にその保有者の売りかは検証できない（他の売り手・買い需要も混在する）。
  あくまで「目安のタイミング」を出すためのヒューリスティックであり、確定情報ではない。
  確定シグナルは「保有割合が5%未満となった変更報告書（報告義務消滅）」の方。
"""
import statistics


def median_baseline(volumes: list) -> float:
    """平常出来高の推定値。外れ値（急増日）に強い中央値を使う。"""
    vals = [v for v in volumes if v]
    if not vals:
        return 0.0
    return float(statistics.median(vals))


def compute_absorption(remaining_shares: float, bars: list, baseline_volume: float,
                       seller_share: float = 1.0,
                       down_day_weight: float = 1.0,
                       up_day_weight: float = 1.0,
                       pace_window: int = 10) -> dict:
    """報告日以降の超過出来高から、残存保有株の消化状況を推定する。

    remaining_shares: 報告書時点の保有株券等の数（株）
    bars: 報告日より後の日足 [(date_str, close, volume), ...]（古い順）
    baseline_volume: 平常時の1日出来高（median_baseline等で算出）
    戻り値: consumed（推定消化株数）/ progress（消化率）/ remaining（推定残存）/
            pace（直近の1日あたり消化ペース）/ days_left（残り営業日の見込み）/
            daily（日次の内訳）
    """
    consumed = 0.0
    daily = []
    prev_close = None
    for date_str, close, volume in bars:
        if volume is None:
            continue
        excess = max(0.0, float(volume) - baseline_volume)
        weight = down_day_weight
        if prev_close is not None and close is not None and close > prev_close:
            weight = up_day_weight
        add = excess * weight * seller_share
        consumed += add
        daily.append({"date": date_str, "volume": volume, "excess": excess,
                      "consumed_delta": add, "consumed_cum": consumed})
        if close is not None:
            prev_close = close

    remaining = max(0.0, remaining_shares - consumed)
    progress = (consumed / remaining_shares) if remaining_shares > 0 else 1.0

    # 直近pace_window日の平均消化ペースから、残りの営業日数を見積もる
    recent = daily[-pace_window:] if daily else []
    pace = statistics.mean([d["consumed_delta"] for d in recent]) if recent else 0.0
    if remaining <= 0:
        days_left = 0
    elif pace > 0:
        days_left = int(remaining / pace + 0.999)
    else:
        days_left = None  # ペースが立たない（超過出来高が出ていない）

    return {"consumed": consumed, "progress": progress, "remaining": remaining,
            "pace": pace, "days_left": days_left, "daily": daily,
            "baseline_volume": baseline_volume}


def evaluate_alerts(holder: dict, absorption: dict, fired: set,
                    progress_tiers: list = None,
                    near_5pct_threshold: float = 6.0,
                    projection_notice_days: int = 5) -> list:
    """通知すべきイベントを判定する。fired は既に通知済みのキー集合（重複通知の抑制）。

    holder: {symbol, filer, shares, ratio, report_date, is_final(5%割れ), ratio_prev}
    戻り値: [{key, kind, ...}] のリスト
    """
    alerts = []
    progress_tiers = progress_tiers if progress_tiers is not None else [0.5, 0.8, 1.0]
    sym, filer = holder["symbol"], holder["filer"]
    ratio = holder.get("ratio")
    base = f"{sym}:{filer}:{holder.get('report_date')}"

    def add(key, kind, **kw):
        if key in fired:
            return
        alerts.append({"key": key, "kind": kind, "symbol": sym, "filer": filer,
                       "ratio": ratio, "report_date": holder.get("report_date"), **kw})

    # 1) 変更報告書の検知（保有割合の低下）
    prev = holder.get("ratio_prev")
    if ratio is not None and prev is not None and ratio < prev:
        add(f"{base}:decrease", "RATIO_DOWN", ratio_prev=prev,
            shares=holder.get("shares"))

    # 2) 5%接近（報告義務消滅が近い＝売り切りが近い）
    if ratio is not None and ratio < near_5pct_threshold and ratio >= 5.0:
        add(f"{base}:near5", "NEAR_5PCT", shares=holder.get("shares"))

    # 3) 5%割れ（報告義務消滅＝売却完了が確定的）
    if holder.get("is_final") or (ratio is not None and ratio < 5.0):
        add(f"{base}:below5", "BELOW_5PCT", shares=holder.get("shares"))

    # 4) 消化進捗（推定）。到達した最上位のtierだけを通知する
    #    （50%/80%/100%を同時に満たしたとき同じ内容を3回出さないため）
    progress = absorption["progress"]
    reached = [t for t in sorted(progress_tiers) if progress >= t]
    if reached:
        tier = reached[-1]
        kind = "ABSORBED_DONE" if tier >= 1.0 else "ABSORB_PROGRESS"
        add(f"{base}:progress{int(tier*100)}", kind, tier=tier,
            progress=progress, consumed=absorption["consumed"],
            remaining=absorption["remaining"], shares=holder.get("shares"))

    # 5) 売り切り接近（見込み日数がN営業日以内）
    dl = absorption["days_left"]
    if dl is not None and 0 < dl <= projection_notice_days and progress < 1.0:
        add(f"{base}:eta", "ETA_SOON", days_left=dl, progress=progress,
            remaining=absorption["remaining"], pace=absorption["pace"])

    return alerts
