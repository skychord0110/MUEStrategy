# runner — 統合ランナー（全ストラテジーを1コマンドで実行）

全ストラテジー（small_lot_sell_detector / panic_sell_detector / under_surge_detector、
およびそれらの検知結果を入力とするAIストラテジー AIStrategys/afternoon_reversal・confluence）を
**1プロセス・1WebSocket接続**でまとめて実行する。kabuステーションへの認証・銘柄登録は
1回だけ行い、受信した各PUSHメッセージを有効化された全検知エンジンに配る。
検知・通知のみで**発注は行わない**。

さらに起動時に、EDINET大量保有報告書モニタと定期買い集め検知(RSS)も
バックグラウンドで一緒に立ち上がる（後述）。

## 3窓並走に対する利点

- 起動が1コマンド・1ウィンドウ。ログも `logs/` に一元化（ストラテジー名付き・日付別ファイル）
- WebSocket接続が1本・銘柄登録が1回になり、起動時の競合が構造的に起きない
- 全ストラテジーが同一のPUSHメッセージ列を同じ順序で処理するため、差分ベースの判定の一貫性が高い
- 新ストラテジーの追加は、detectorクラスを書いて本ランナーに登録するだけ

各ストラテジーの単体実行（`strategies/<名前>/src/main.py`）も引き続き可能
（単体デバッグ用。統合ランナーと同時に起動しないこと — 銘柄登録が競合する）。

## 実行手順

**画面から操作したい場合**は、リポジトリ直下の `コントロールパネル.bat` をダブルクリックする。
起動・停止・ストラテジーのON/OFF・銘柄入れ替え・評価損益の表示までまとめて行える。
詳細は [../../ui/README.md](../../ui/README.md)。以下はコマンドで動かす場合の手順。

```powershell
# 初回のみ
cd strategies\runner
pip install -r requirements.txt
# config.yaml は作成済み（config.example.yamlと同内容）。閾値を変える場合はここを編集

# 初回のみ: パスワード・APIキーを永続化する（下記「認証情報の設定」参照）
setx KABU_API_PASSWORD "本番用APIパスワード"
setx EDINET_API_KEY "取得したEDINET APIキー"

# 毎回はこれだけ
cd src
python main.py --config ../config.yaml
```

前提: kabuステーション（デスクトップアプリ）が起動・ログイン済みで、API設定が有効なこと。
監視銘柄は全ストラテジー共通の [../symbols.yaml](../symbols.yaml) で管理する。
停止は `Ctrl + C`（またはコントロールパネルの「停止」）。

### 停止のしくみ

`Ctrl + C` のほかに、**`state/stop.request` というファイルを置くと終了する**。
コントロールパネルからの停止はこちらを使っている。
Windowsでは窓なしで起動した子プロセスが親と別のコンソールを持つため、
コンソール制御イベント（Ctrl+Break）が届かず、強制終了しか手段がなくなるため。

どちらの経路でも、WebSocketを閉じてログを書き終えてから終了する（終了コード0）。
`state/account.json`（下記）は終了時に削除される。銘柄登録は解除しない。

### state/ ディレクトリ

- `account.json` … 買付余力・建玉・評価損益のスナップショット。既定15秒ごとに更新。
  コントロールパネルの表示用で、`config.yaml` の `account_snapshot` で設定する。
  **UI側でAPIトークンを発行しないための仕組み**（`/token` は「別のトークンが
  新たに発行された時」に既存トークンを無効化するため、UIが独自に認証すると
  稼働中ランナーの発注が失敗しうる。詳細: `src/account_snapshot.py`）
- `stop.request` … 停止要求。ランナーが検知して自分で削除する。

いずれもgit管理外。

## 認証情報の設定（毎回入力しないために）

`$env:KABU_API_PASSWORD = "..."` はそのターミナルを閉じると消えるため毎回入力が必要になる。
`setx` を使うと**Windowsのユーザー環境変数として永続化**され、以後は入力不要になる。

```powershell
setx KABU_API_PASSWORD "本番用APIパスワード"
setx EDINET_API_KEY "取得したEDINET APIキー"
```

- **一度だけ実行すればよい**。設定は**新しく開いたターミナルから有効**になる
  （実行中のターミナルには反映されないので、一度閉じて開き直すこと）
- 確認: `echo $env:KABU_API_PASSWORD`（新しいターミナルで）
- 変更: 同じ `setx` をやり直す。削除: `Remove-ItemProperty -Path HKCU:\Environment -Name KABU_API_PASSWORD`

**注意**: `setx` の値はレジストリ（`HKCU:\Environment`）に**平文で保存される**。
自分専用のPCであれば通常問題ないが、共用PCでは避けること。
なお、**パスワードをconfigやREADMEなどgit管理下のファイルに書いてはいけない**
（リポジトリには `.gitignore` を用意済みだが、そもそも書かない運用にする）。

より安全にしたい場合は Windows資格情報マネージャー（`keyring` ライブラリ経由）に
保管する方式にもできる。必要なら対応可能。

## ログの保存先

ログは `runner/logs/` ディレクトリに**日付ごとのファイル**で保存される（ディレクトリは自動作成）:

```
runner/logs/
├── runner_2026-07-09.log
├── runner_2026-07-10.log
└── runner_2026-07-13.log   ← 本日分（起動中に日付が変わっても自動で切り替わる）
```

過去分の振り返りはファイル名の日付で探せる。コンソールにも同じ内容が表示される。

## config.yaml の構成

- `environment` / `symbols_file` / `debug_raw_messages`: 接続・銘柄・デバッグ設定（従来と同じ）
- `strategies.<ストラテジー名>.enabled`: そのストラテジーの有効/無効。
  `false` にすれば他を動かしたまま1つだけ止められる
- 各ストラテジーの閾値パラメータは、単体版のconfig.yamlと同じキー名でこのファイルに集約

**注意: 閾値の設定は統合ランナーでは `runner/config.yaml` が使われる。**
単体版の `strategies/<名前>/config.yaml` の変更は統合ランナーには反映されない。

## 通知の見分け方

通知タイトルの先頭にストラテジーラベルが付く:

- `[小口売り連続/WATCH] 4165` / `[小口売り連続/STRONG] 4165`
- `[投げ売り/買い気配へぶつけ] 4165` / `[投げ売り/投げ売り吸収] 4165`
- `[UNDER急増] 4165`
- `[AI午後引け戻り/エントリー] 4165` / `[AI午後引け戻り/決済:利確] 4165`（仮想売買・発注なし）
- `[AI複合シグナル/エントリー] 4165` / `[AI複合シグナル/決済:大引け] 4165`（仮想売買・発注なし）
- `[AI投げ売り反発/エントリー] 4813` / `[AI投げ売り反発/決済:利確] 4813`（仮想売買・発注なし）
- `[アルゴ買い集め(RSS)/WATCH] 4013` / `[アルゴ買い集め(RSS)/STRONG] 4013`（歩み値ベース・別系統）
- `[大量保有/5%割れ] 4446` など（EDINETモニタ・別系統）

AIストラテジー（仮想売買）の詳細は [../AIStrategys/README.md](../AIStrategys/README.md) を参照。

## 仕組み（新ストラテジー追加時の参考）

- 各ストラテジーの `src/detector.py` を `importlib` で個別に読み込むため、
  モジュール名の衝突なく既存コードを無変更で流用している
- 追加手順: (1) 新ストラテジーの detector.py を作る → (2) `runner/src/main.py` の
  `RunnerEngine.__init__` にロード処理、`handle()` に配信処理を追加 →
  (3) `runner/config.yaml` にパラメータセクションを追加 →
  (4) `runner/src/notifier.py` の `build_message` に通知フォーマットを追加

## 同時起動: EDINET大量保有報告書モニタ

`runner/config.yaml` の `edinet_holder_monitor.enabled: true` により、ランナー起動時に
[../edinet_holder_monitor/](../edinet_holder_monitor/README.md) が**バックグラウンドで
一緒に起動**する（`python main.py --config ../config.yaml` だけでよい）。

- 大口保有者の売却進捗を追い、5%割れ・売り切り推定などを通知する
- ランナーは常時稼働のWebSocketループ、モニタは日次バッチなので、**別スレッド**で
  「起動時に1回 → 以降 `interval_hours` ごと」に実行する。PUSH処理はブロックしない
- 通知・ログはランナー側に一元化される（`runner/logs/` に出る）
- **要: 環境変数 `EDINET_API_KEY`**。未設定なら警告を出してスキップし、
  ランナー本体はそのまま動作する。EDINET側でエラーが起きてもランナーは落ちない

```powershell
setx EDINET_API_KEY "取得したAPIキー"   # 一度だけ。新しいターミナルから有効
```

止めたい場合は `edinet_holder_monitor.enabled: false`。単体実行も従来どおり可能。

## 同時起動: 定期買い集め検知（楽天マーケットスピードII RSS版）

`runner/config.yaml` の `periodic_buy_rss.enabled: true` により、ランナー起動時に
[../periodic_buy_rss/](../periodic_buy_rss/README.md) も**バックグラウンドで一緒に起動**する
（`python main.py --config ../config.yaml` だけでよい）。

- 歩み値上で「**約定の丁度10秒後に買われる**」動きが1日に何度も出る銘柄を通知する
  （光通信のような事業会社が買い集めているサインの疑い）
- データ源が楽天証券のため、kabuのPUSHとは**別スレッド**で独自のポーリングループを回す。
  PUSH処理はブロックしない。通知・ログはランナー側に一元化される
- **Excel/MS2が未起動でも落ちない**。`connect_retry_seconds` 間隔で
  `max_connect_retries` 回まで再試行するので、ランナーを先に起動してから
  MS2/Excelを立ち上げる運用でも拾える。それでも駄目なら警告を出して諦め、
  ランナー本体は動作を継続する

**ユーザー側で用意しておくこと**（詳細は上記READMEを参照）:

1. **マーケットスピードII** を起動しログインしておく（RSSは起動中のみ更新される）
2. **Excel に「マーケットスピードII RSS」アドインを有効化**しておく
   （楽天証券のRSS設定手順に従う。Excelは通常のデスクトップ版・Windows）
3. **空のExcelブックを1つ開いておく**（本ツールがそこに `TICKS` シートを作り数式を書き込む）

止めたい場合は `periodic_buy_rss.enabled: false`。単体実行も従来どおり可能。
