# runner — 統合ランナー（全ストラテジーを1コマンドで実行）

全ストラテジー（small_lot_sell_detector / panic_sell_detector / under_surge_detector）を
**1プロセス・1WebSocket接続**でまとめて実行する。kabuステーションへの認証・銘柄登録は
1回だけ行い、受信した各PUSHメッセージを有効化された全検知エンジンに配る。
検知・通知のみで**発注は行わない**。

## 3窓並走に対する利点

- 起動が1コマンド・1ウィンドウ。ログも `logs/` に一元化（ストラテジー名付き・日付別ファイル）
- WebSocket接続が1本・銘柄登録が1回になり、起動時の競合が構造的に起きない
- 全ストラテジーが同一のPUSHメッセージ列を同じ順序で処理するため、差分ベースの判定の一貫性が高い
- 新ストラテジーの追加は、detectorクラスを書いて本ランナーに登録するだけ

各ストラテジーの単体実行（`strategies/<名前>/src/main.py`）も引き続き可能
（単体デバッグ用。統合ランナーと同時に起動しないこと — 銘柄登録が競合する）。

## 実行手順

```powershell
# 初回のみ
cd strategies\runner
pip install -r requirements.txt
# config.yaml は作成済み（config.example.yamlと同内容）。閾値を変える場合はここを編集

# 毎回
$env:KABU_API_PASSWORD = "本番用APIパスワード"
cd src
python main.py --config ../config.yaml
```

前提: kabuステーション（デスクトップアプリ）が起動・ログイン済みで、API設定が有効なこと。
監視銘柄は全ストラテジー共通の [../symbols.yaml](../symbols.yaml) で管理する。
停止は `Ctrl + C`。

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

## 仕組み（新ストラテジー追加時の参考）

- 各ストラテジーの `src/detector.py` を `importlib` で個別に読み込むため、
  モジュール名の衝突なく既存コードを無変更で流用している
- 追加手順: (1) 新ストラテジーの detector.py を作る → (2) `runner/src/main.py` の
  `RunnerEngine.__init__` にロード処理、`handle()` に配信処理を追加 →
  (3) `runner/config.yaml` にパラメータセクションを追加 →
  (4) `runner/src/notifier.py` の `build_message` に通知フォーマットを追加
