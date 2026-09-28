# spam-hunter

Twitch のライブ配信チャットを記録し、[Laya](https://github.com/NandhaKishorM/laya) による荒らし判定が日本語の配信チャットでどのくらい当たるかを評価するツールです。

- 設計書: [`docs/design.md`](docs/design.md)（第2版）／ 元の設計書: [`docs/design-v1.md`](docs/design-v1.md)
- 進み具合: **M1（受信・記録・ルール判定）** まで実装済み

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

必要なら、リポジトリ直下で `.env.example` を `.env` にコピーして値を入れます（M1 では、どれも入れなくても動きます）。

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

## テスト

```bash
cd backend
python -m pytest
```

テストはローカルの偽の IRC サーバーを使うので、Twitch には接続しません。
