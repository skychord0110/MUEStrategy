# panic_sell_detector — OVER売り降ろし（投げ売り）検知

当日安値圏で、板のOVER（売り気配10本より上に隠れている売り注文の合計）が一気に減少し、
それとほぼ同数の売りが市場に降りてきた瞬間 — **上で待っていた大口売り手が諦めて投げに来た局面** —
を検知してポップアップ通知するツール。検知・通知のみで**発注は行わない**。

kabuステーションAPI（三菱UFJ eスマート証券）のPUSH配信を使用する。
- APIリファレンス: https://kabucom.github.io/kabusapi/reference/index.html
- PUSH配信仕様: https://kabucom.github.io/kabusapi/ptal/push.html

## 検知フロー

```
前提条件: 現在値が当日安値から low_zone_pct（既定0.7%）以内の安値圏にいる
    ↓
イベント: OverSellQty が1回の更新で急減
  - OVERが large_over_threshold（既定10万株）以下の銘柄: over_drop_threshold（既定3,000株）以上の減少
  - OVERが10万株を超える銘柄: 直前のOVERの large_over_drop_pct（既定10%）以上の減少
    ↓ （以後 match_window_seconds（既定30秒）の間、監視）
    ├─ ケースA: 減少量の80%以上が売り気配周辺（Sell1〜requote_levels）に上乗せ（内部判定のみ・通知なし）
    │     さらに減少量の requote_consumed_fraction（既定50%）が買い方起点の約定で消化
    │     → 通知 [ABSORBED 投げ売り吸収]
    │
    └─ ケースB: 減少量の80%以上が売り方起点の約定として出来高に出現
          → 通知 [DUMP 買い気配へぶつけ]
```

「ほぼ同数」の判定は `match_tolerance`（既定0.2 = ±20%）で調整する。

## なぜ「同数の突き合わせ」が必須か

OVERの減少だけでは、
- 売り注文の**取り消し**（売り手が売る気をなくした = むしろ強気材料）
- 売り注文の**降ろし直し**（今すぐ売りたい = 投げ売り）

を区別できない。減少量とほぼ同数が「売り気配周辺への指し直し」または
「買い気配へのぶつけ」として現れたことを確認して初めて後者と判断する。

## 板の窓シフトによる偽検知への対策

価格が動くと表示10本の窓がずれ、OVERとの間で注文が機械的に出入りする。

- 窓が**上**にずれてOVERから現れる注文は売り気配の**上のほう**（Sell7〜10側）に出現する。
  そのため「売り直し」の判定は最良売り気配に近い**下位 `requote_levels` 本（既定3本）**への
  数量増加のみを数え、機械的な出現を除外している。
- 数量の比較は**価格ごとの辞書照合**で行うため、表示位置（Sell3→Sell1等）のずれの影響を受けない。
- 価格が**下**にずれて窓の下端に現れる新しい売り気配は、OVERに隠れていたものではあり得ない
  （OVERは窓の上側のみ）ため、新規の売り指値としてそのまま数えてよい。

## フィールド名の注意（実データで確認済み）

kabuステーションAPIは `BidPrice`=最良「売」気配 / `AskPrice`=最良「買」気配という、
一般的な英語の慣例と**逆**の命名（2026-07-07の実データで `BidPrice`=`Sell1.Price`、
`AskPrice`=`Buy1.Price` の一致を確認済み）。本ツールでは誤解の余地がない
`Sell1〜Sell10` / `Buy1` を使用する。

## 重要な前提・限界

- PUSH配信には歩み値（1約定ごとのデータ）がないため、**スナップショット間の差分による近似**。
  短時間に複数の変化が1回のPUSHに合算されると精度が落ちる。
- 約定の売買方向（売り方起点/買い方起点）は「直前のPUSH時点の最良気配」との価格比較による推定。
- 複数のOVER急減イベントが時間窓内で重なった場合、同じ約定・板変化が複数イベントに
  重複して算入されることがある（過剰通知側に倒れる）。
- 寄り付き・引け・昼休み明けの板寄せ前後は板が大きく組み変わるため、誤検知が出やすい。
  通知時刻が9:00直後・12:30直後のものは割り引いて評価すること。
- 検証環境（demo）は発注テスト専用で市場データが流れない（実挙動で確認済み）。
  本ツールは `environment: production` で使う（発注機能がないため資金は動かない）。

## セットアップと起動

```powershell
cd strategies\panic_sell_detector
pip install -r requirements.txt
copy config.example.yaml config.yaml
# config.yaml の銘柄・閾値を編集

$env:KABU_API_PASSWORD = "本番用APIパスワード"
cd src
python main.py --config ../config.yaml
```

前提: kabuステーション（デスクトップアプリ）が起動・ログイン済みで、API設定が有効なこと。
ログはコンソールと `src\panic_sell_detector.log` に出力される。

## パラメータ一覧（config.yaml）

| キー | 既定値 | 意味 |
|---|---|---|
| `over_drop_threshold` | 3000 | OVER急減イベントとみなす1更新あたりの減少株数（OVER小銘柄向け） |
| `large_over_threshold` | 100000 | このOVER株数を超える銘柄は割合判定に切り替え |
| `large_over_drop_pct` | 0.10 | OVER大銘柄向け: 直前のOVERに対する減少割合の閾値 |
| `low_zone_pct` | 0.007 | 当日安値からこの割合以内を「安値圏」とする |
| `match_tolerance` | 0.2 | 「ほぼ同数」の許容誤差（減少量の80%以上で成立） |
| `match_window_seconds` | 30 | イベント発生後の監視時間窓 |
| `requote_levels` | 3 | 「売り気配周辺」とみなす板の本数 |
| `requote_consumed_fraction` | 0.5 | ABSORBED通知に必要な消化割合 |
| `price_tolerance_ticks` | 0 | 売買方向分類の価格許容ティック数 |
