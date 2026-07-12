# under_surge_detector — UNDER急増（下値への大口買い出現）検知

当日安値圏（下落局面）で、板のUNDER（買い気配10本より下に隠れている買い注文の合計
= `UnderBuyQty`）が一気に20%以上増加し、かつOVER（売り側 = `OverSellQty`）がほぼ
変わっていない場合に通知する。**下値に大口の買い指値がまとめて置かれた（買い支え出現）**
局面の検知を狙う。検知・通知のみで**発注は行わない**。

kabuステーションAPI（三菱UFJ eスマート証券）のPUSH配信を使用する。
- APIリファレンス: https://kabucom.github.io/kabusapi/reference/index.html
- PUSH配信仕様: https://kabucom.github.io/kabusapi/ptal/push.html

## 検知条件（すべてANDで成立時に通知）

```
1. 現在値が当日安値から low_zone_pct（既定1%）以内の安値圏にいる
2. UnderBuyQty が1回のPUSH更新間で直前の値の under_increase_pct（既定20%）以上増加
3. 同じ更新間で OverSellQty の変化が over_change_tolerance_pct（既定±5%）以内
   （売り側は動いておらず、純粋に買いだけが積まれたことの確認）
4. 同一銘柄の直前の通知から cooldown_seconds（既定60秒）以上経過している
5. 板寄せ直後の quiet_windows（既定: 9:00〜9:01、12:30〜12:31）の時間帯でない
```

## 板の窓シフトとの関係（設計根拠）

価格が下落すると表示10本の買い窓が下にずれ、UNDERにあった注文が表示側に吸われるため、
UnderBuyQtyは機械的には**減る**方向にドリフトする。下落中の20%急増はこのドリフトに
逆らう動きであり、新規の買い注文が実際に置かれたことを強く示唆する（誤検知しにくい方向）。

逆に価格上昇中は、表示側の買い気配が窓から外れてUNDERに流入し機械的に増えるが、
安値圏条件（条件1）でこのケースは除外される。

OVER不変条件（条件3）は、売り買い両方が同時に動く「板全体の組み変わり」
（寄り前の注文集中、機関の板の入れ替え等）を除外するためのもの。

## 重要な前提・限界

- PUSH配信のスナップショット差分による近似。短時間の複数の変化が1回のPUSHに
  合算される場合がある。
- UNDERの増加は「見せ板（約定させる意図のない大口買い注文）」の可能性もある。
  通知されたら、その後その買い注文が維持されるか（すぐ取り消されたら見せ板の疑い）を
  板画面で確認すること。
- 寄り付き・後場寄り直後（9:00〜9:01、12:30〜12:31）は quiet_windows により通知を自動抑制する。
  それ以外でも引け間際など板が大きく動く時間帯の通知は割り引いて評価すること。
- 検証環境（demo）は発注テスト専用で市場データが流れない（実挙動で確認済み）。
  本ツールは `environment: production` で使う（発注機能がないため資金は動かない）。

## セットアップと起動

```powershell
cd strategies\under_surge_detector
pip install -r requirements.txt
copy config.example.yaml config.yaml
# config.yaml の銘柄・閾値を編集

$env:KABU_API_PASSWORD = "本番用APIパスワード"
cd src
python main.py --config ../config.yaml
```

前提: kabuステーション（デスクトップアプリ）が起動・ログイン済みで、API設定が有効なこと。
ログはコンソールと `src\under_surge_detector.log` に出力される。

## パラメータ一覧（config.yaml）

| キー | 既定値 | 意味 |
|---|---|---|
| `under_increase_pct` | 0.20 | UNDER急増とみなす1更新あたりの増加割合 |
| `over_change_tolerance_pct` | 0.05 | 「OVERほぼ不変」の許容変化割合 |
| `low_zone_pct` | 0.01 | 当日安値からこの割合以内を「安値圏」とする |
| `cooldown_seconds` | 60 | 同一銘柄の連続通知の抑制間隔（秒） |
| `quiet_windows` | 09:00-09:01, 12:30-12:31 | 通知を抑制する時間帯（板寄せ直後） |
