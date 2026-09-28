# spam-hunter

Twitch のライブ配信チャットを記録し、[Laya](https://github.com/NandhaKishorM/laya) による荒らし判定が日本語の配信チャットでどのくらい当たるかを評価するツールです。

- 設計書: [`docs/design.md`](docs/design.md)（第2版）／ 元の設計書: [`docs/design-v1.md`](docs/design-v1.md)
- 進み具合: **M3（セッション後の流れと通知）** まで実装済み

## セットアップ

Python 3.12 を使います。

```powershell
# Windows（PowerShell）
cd backend
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

```bash
# Linux / macOS
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Laya で判定する（M2）には、判定用の依存パッケージも入れます（torch などを含むので、数GB あります）。

```bash
pip install -e ".[judge,dev]"
```

リポジトリ直下で `.env.example` を `.env` にコピーして、使う機能の値を入れます（どれも入れなくても、監視と判定は動きます）。

| 値 | 入れると使える機能 | 取り方 |
|---|---|---|
| `TWITCH_CLIENT_ID`・`TWITCH_CLIENT_SECRET` | 配信状況（ゲーム名・タイトル）の自動取得、配信が終わったら自動で監視を止める | Twitch の開発者コンソール（dev.twitch.tv）でアプリを登録する |
| `DISCORD_WEBHOOK_URL` | ルールの印のリアルタイム通知、セッション後のまとめ通知 | Discord のチャンネルの設定 →「連携サービス」→「ウェブフック」で作る |

`.env` は Git に入らないようになっています。中身を人に送ったり、画面に映したりしないでください。

## 使い方（M1: ターミナルで監視する）

`backend/` で実行します。

```bash
# ②視聴者の多い配信の枠
python -m app.cli monitor <チャンネル名> --purpose high_traffic

# ①ストーリー重視ゲームの初見プレイの枠（指示を歓迎しているかの入力が必須）
python -m app.cli monitor <チャンネル名> --purpose story_firstplay --welcomes-advice no \
    --game "ゲーム名" --spoiler-note "ストーリー初見プレイ、第3章まで"
```

- 受信したコメントが1件ずつ表示され、`data/moderation.db` に保存されます。連投と URL には `[repeat]`・`[url]` の印が付きます。
- Ctrl+C で止めると、セッションを閉じてから終了します。
- 切断されたら、1秒・2秒・4秒…（上限60秒）と待ち時間を延ばしながら自動で再接続します。
- 表示名とユーザー ID は保存しません。投稿者は、PC ごとのソルトによる仮名 ID で保存します（ソルトは `data/pseudonym_salt`）。

主なオプション：

| オプション | 内容 |
|---|---|
| `--purpose` | 配信の枠。`story_firstplay`／`high_traffic`／`other`（必須） |
| `--welcomes-advice` | 指示を歓迎しているか。`yes`／`no`／`unknown`（既定は `unknown`） |
| `--game`・`--stream-title`・`--spoiler-note` | 配信状況の手入力 |
| `--repeat-window`・`--repeat-count` | 連投とみなす秒数と回数（既定は60秒・3回） |
| `--db` | SQLite のパス |
| `--quiet` | コメントを表示しない |
| `--no-pipeline` | 監視を止めたあと、判定に進まない |
| `--no-notify` | Discord に通知しない |

- 監視を止める（Ctrl+C、または配信の終了を自動で検知）と、そのまま「判定 → 層別抽出 → まとめ通知」に進みます。進ませたくないときは `--no-pipeline` を付けます。
- 連投と URL の印は、Discord にすぐ通知されます。同じ投稿者の2回目以降は、5分ごとに件数をまとめて通知します。

## 使い方（判定・抽出・まとめ通知）

監視が終わったセッションを、`config/questions.yaml` の本番の variant（`primary`）で判定し、採点キューへの抽出とまとめ通知まで進めます。`backend/` で実行します。

```bash
python -m app.cli pipeline                  # 待っているセッションをすべて進める（途中で止めたものの続きも含む）
python -m app.cli pipeline --session 3      # セッションを指定する
python -m app.cli judge                     # 判定だけを行う（抽出と通知はしない）
```

- 初回は Laya の多言語用モデル（約650MB）をダウンロードします。
- 1回目の Ctrl+C で、いまのバッチを終えてから止まります。もう一度 `pipeline` を実行すると、続きから進みます。
- 抽出の件数、通知の設定、ルールの値などの初期値は `config/defaults.yaml` にあります。
- 質問の文言と variant は `config/questions.yaml` で変えられます。評価期間中は、本番の variant（`primary`）を変えないでください。

### ONNX 版を使う・比べる

```bash
python scripts/export_onnx.py      # backend/models/ に fp32 版と INT8 版を書き出す（合わせて約2.2GB）
```

### 処理速度を測る

```bash
python scripts/bench.py                                        # torch と onnx-int8、バッチ 1/4/8、2スレッド
python scripts/bench.py --backends torch onnx onnx-int8 --json bench.json
```

1件あたりの判定時間（7項目）、1万件にかかる時間の見込み、PyTorch 版との判定の差が表示されます。測定には自作のサンプルのコメントを使います。

## テスト

```bash
cd backend
python -m pytest
```

テストはローカルの偽の IRC サーバーと偽の判定器を使うので、Twitch には接続せず、Laya のモデルも使いません。本物の Laya を使うテストは、次のように実行します（モデルのダウンロードが必要）。

```bash
python -m pytest -m slow
```
