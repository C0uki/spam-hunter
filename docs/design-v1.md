# Twitch チャット荒らし検知ツール 設計書

> この設計書は Claude Code で実装することを前提にしています。
> 「【要確認】」が付いている箇所は、実装時に公式ドキュメントや実際の API レスポンスを見て確かめてから作ってください。

---

## 1. 目的とスコープ

### 目的
Twitch のライブ配信チャットを受信し、Laya（非自己回帰型の判定エンジン）で荒らし・不適切発言を判定します。怪しいコメントは Discord に通知し、人が最終判断します。判定結果はダッシュボードで採点し、**Laya が日本語の配信チャットでどのくらい当たるか**を数字で確かめます。

### スコープ（作るもの）
- Twitch の公開配信チャットの受信（同時に1配信）
- ルールによる判定（連投・URL）と、Laya による7項目の判定
- Discord Webhook への通知
- SQLite への保存と、保持期限による自動削除
- ローカルで動く Web ダッシュボード（一覧・採点・統計・設定）

### 非スコープ（作らないもの）
- コメントの自動削除やタイムアウト（モデレーター操作）は行わない。**通知のみ**。
- YouTube 対応（将来の拡張。§13 を参照）
- 複数配信の同時監視（将来の拡張。ただし拡張しやすい構造にしておく）
- クラウドへのデプロイ（Vercel などは使わない）

### 運用の位置づけ
試験運用です。特定の配信者ではなく、公開配信で試します。

---

## 2. 前提と動作環境

| 項目 | 内容 |
|---|---|
| 実行 PC | Surface Go 2（RAM 8GB、2コア CPU、GPU なし、Windows）。**学校の端末なので、常用する前に使用許可を確認すること** |
| Python | 3.12（`py -3.12` で作成した venv） |
| Laya | 0.3.21 で動作確認済み。多言語用モデル（`convaiinnovations/laya` の `multilingual`、約650MB）はダウンロード済み |
| Node.js | フロントエンドのビルド時だけ使う。動かすときは不要 |
| 起動するプロセス | FastAPI の Python プロセス1つだけ。受信・判定・通知・API・画面の配信をすべてこの中で行う |
| 公開範囲 | `127.0.0.1` でだけ待ち受け、同じ PC のブラウザからだけ見られるようにする |

---

## 3. 全体の構成

```
 Twitch チャット（IRC over WebSocket、読み取り専用の匿名接続）
        │ メッセージ
        ▼
 ┌─────────────────────── FastAPI プロセス（asyncio） ───────────────────────┐
 │                                                                         │
 │  [受信] ──▶ [ルール判定: 連投・URL] ──▶ [判定キュー]                           │
 │                                            │ まとめて取り出す（ミニバッチ）       │
 │                                            ▼                            │
 │                                   [Laya 判定ワーカー]                      │
 │                                   （専用スレッド、predict_batch）            │
 │                                            │                            │
 │                                            ▼                            │
 │                                   [通知するかの判定]                        │
 │                          ┌─────────────────┼─────────────────┐          │
 │                          ▼                 ▼                 ▼          │
 │                   [Discord 通知]     [SQLite 保存]    [WebSocket 配信]     │
 │                                                            │            │
 │  [配信状況] ◀── Twitch Helix API（ゲーム名・タイトル） ＋ 手入力           │            │
 │  [保持期限の削除ジョブ]                                          │            │
 └────────────────────────────────────────────────────────────┼────────────┘
                                                              ▼
                                  ダッシュボード（React。ビルド済みの静的ファイルを FastAPI が配信）
```

- Laya の推論は重い CPU 処理です。イベントループを止めないよう、**専用スレッド1本**（`run_in_executor` など）で実行します。
- 各部品は「配信1つ分」を単位にしたクラスにまとめておきます。こうしておくと、将来は複数配信に広げやすくなります（§13）。

---

## 4. 各部の詳細

### 4.1 Twitch チャットの受信
- 接続先は Twitch チャットの IRC（WebSocket 版）です。読むだけなら**匿名ログイン**（`justinfan` で始まるニックネーム）で接続できます。【要確認】接続先 URL と匿名ログインの手順
- `twitch.tv/tags` などの capability を要求して、次の情報をタグから取ります。【要確認】タグ名
  - メッセージ ID
  - 投稿者 ID
  - 表示名
  - 送信時刻
- 切断されたら、少しずつ待ち時間を延ばしながら（指数バックオフ）自動で再接続します。
- ダッシュボードから、見張るチャンネル（ログイン名）の指定・開始・停止ができるようにします。

### 4.2 配信状況（判定の文脈）
ネタバレと指示厨の判定には、配信の状況が必要です。

| 項目 | 取得方法 |
|---|---|
| ゲーム名（カテゴリ） | Twitch Helix API の配信情報（`GET /helix/streams?user_login=...` の `game_name`）から**自動取得**します。Client ID とアプリ用アクセストークン（Client Credentials）が必要です。【要確認】 |
| 配信タイトル | 同じ API の `title` から自動取得します |
| 指示を歓迎しているか | ダッシュボードで**手入力**します（はい／いいえ／不明。既定は不明） |
| ネタバレ注意メモ | ダッシュボードで**手入力**します（任意。例：「ストーリー初見プレイ、第3章まで」） |

- 自動取得は、監視を始めたときと、その後は定期的（例：5分ごと）に行います。
- API キーが設定されていなければ自動取得は行わず、手入力だけで動くようにします。

### 4.3 ルールによる判定（Laya を使わない）
コメント1件だけでは分からないものは、プログラムで判定します。

| ルール | 初期値（設定画面で変更できる） |
|---|---|
| 連投 | 同じ投稿者が、同じ内容（正規化後）を **60秒以内に3回以上** 書いたら「連投」にする |
| URL 投稿 | URL を含むコメントに印を付ける |

- 正規化の内容：全角・半角の統一、空白の除去、同じ文字が並んだ部分の圧縮（例：「wwwww」→「ww」）
- 投稿者の区別は、監視中のメモリ上でだけ行います（§4.7 を参照）。
- 初期状態では、ルール判定の結果は**記録と画面表示のみ**で、Discord には通知しません（設定で通知をオンにできます）。

### 4.4 Laya による判定

#### モデルと読み込み
- `laya.load("convaiinnovations/laya", subfolder="multilingual")` で多言語用モデルを**起動時に1回だけ**読み込みます。日本語しか扱わないので、ルーター（Router）は使いません。
- CPU のスレッド数は物理コア数（2）に合わせます（`torch.set_num_threads(2)` など）。
- 【要確認】インストール済み版（0.3.21）の API が、README の記述（`predict`・`predict_batch`・回答の形式）と一致しているか

#### Laya に渡す入力（state）
```json
{
  "comment": "コメント本文",
  "game": "ゲーム名（取れなければ空）",
  "stream_title": "配信タイトル",
  "streamer_welcomes_advice": "いいえ",
  "spoiler_note": "ストーリー初見プレイ、第3章まで"
}
```

#### 質問の定義（7項目、1回の推論でまとめて判定）

| ID | 種類 | 内容 | 通知の対象 |
|---|---|---|---|
| `abuse` | noul | 暴言・誹謗中傷を含むか | ○ |
| `personal_attack` | noul | 配信者や他の視聴者への個人攻撃か | ○ |
| `spam_promo` | noul | 宣伝・スパムか | ○ |
| `sexual` | noul | 性的・不適切な内容か | ○ |
| `spoiler` | noul | 配信中のゲームのネタバレか（`game` と `spoiler_note` を参照） | 記録のみ |
| `backseat` | noul | 頼まれていないゲームの指示（指示厨）か（`streamer_welcomes_advice` を参照） | 記録のみ |
| `severity` | choice | 荒らしとしての深刻度。`none`（問題なし）／`caution`（注意）／`severe`（悪質） | ○ |

- 質問の文言（`instructions` と `criteria`）は日本語で書き、**設定ファイル（YAML）に外出し**します。コードを変えずに調整できるようにするためです。
- 例（`abuse`）：
  ```yaml
  abuse:
    type: noul
    instructions: "このコメントは暴言や誹謗中傷を含みますか？"
    criteria:
      "false": "普通の感想・質問・雑談で、暴言や誹謗中傷を含まない"
      "true": "罵倒、侮辱、差別的な表現、誹謗中傷を含む"
  ```
- `severity` は `score` 型ではなく `choice` 型にします。README によると、多言語用モデルには `score` 型の質問で**最初の段階を選びにくい偏り**が報告されているためです（issue #131）。選択肢のキーには、true/false や yes/no のような真偽を表す語を使いません。
- `noul` 型には、モデルが選択肢の名前（false/true）に引きずられる偏りが報告されています（issue #156、主に英語用モデル）。そこで、質問ごとに `labels`（例：`{"false": "B", "true": "A"}`）を指定できるようにしておき、採点データを見て切り替えられるようにします。

#### まとめて判定（ミニバッチ）
- キューから**最大8件、または最初の1件が入ってから2秒たった時点**のどちらか早いほうで取り出し、`predict_batch(states, questions)` で判定します（どちらの値も設定で変えられます）。
- 1件ごとに次の2つを記録し、統計画面に出します。
  - 判定にかかった時間
  - キューで待った時間

### 4.5 通知するかの判定（初期値。設定画面で変更できる）
次のどれかに当てはまったら Discord に通知します。

1. `severity` の判定結果が `severe` のとき
2. `abuse`・`personal_attack`・`spam_promo`・`sexual` のどれかで、P(true) が **0.90 以上**のとき

- `spoiler` と `backseat` は通知せず、記録するだけにします（設定で通知の対象に加えられるようにしておきます）。
- 多言語用モデルは確信度が**調整（キャリブレーション）されていない**ので、しきい値は採点データを見て見直す前提です（§4.8 の統計画面を参照）。

### 4.6 Discord 通知
- Discord Webhook の URL に POST します。Embed を使って、見やすい形にします。
- 通知に含める内容：
  - チャンネル名
  - 投稿者の表示名
  - コメント本文
  - 当てはまった項目と確率
  - 深刻度
  - 時刻
- Webhook には送信回数の制限（レート制限）があります。そのため送信用のキューを用意し、429 エラーが返ってきたら `retry_after` の時間だけ待ってから送り直します。短い時間に通知が集中したときは、1通にまとめて送れるようにしておきます。【要確認】レート制限の仕様

### 4.7 保存と保持期限（SQLite）
- 保存先は SQLite の1ファイル（例：`data/moderation.db`）です。
- **投稿者の扱い**
  - 表示名とユーザー ID は、**データベースには保存しません**。
  - ユーザー ID は、PC ごとに作った秘密の値（ソルト）を使って HMAC-SHA256 で変換した**仮名 ID**として保存します。ソルトは初回起動時に自動で作り、`.env` か `data/` に置きます。
  - 表示名はメモリ上のリアルタイム表示と Discord 通知にだけ使います。
- **保持期限**
  - 採点されていないメッセージは、**30日たったら自動で削除**します。
  - 採点済みのメッセージは残します（投稿者は仮名 ID のみ）。
  - 削除ジョブは、起動したときと、その後24時間ごとに実行します。

### 4.8 ダッシュボード（Vite ＋ React ＋ TypeScript）

| 画面 | 機能 |
|---|---|
| **ライブ** | 流れてくるコメントと判定結果をリアルタイムで表示します（WebSocket）。通知対象は色分けします。各行に採点ボタンを付けます |
| **採点** | 未採点・通知済みなどで絞り込んだ一覧で、項目ごとに「判定は正しい／間違い」を付けます。`severity` は正しい段階を選び直せるようにします。「全部正しい」ボタンで一括採点もできます。キーボードのショートカットで素早く採点できるようにします |
| **統計** | 時間帯ごと・項目ごとの件数、採点済みデータから出す項目ごとの正解率（適合率・再現率）、**しきい値を変えたら通知件数と誤った通知がどう変わるか**のシミュレーション、判定時間と待ち時間の推移（p50 と p95） |
| **設定** | 見張るチャンネルの指定・開始・停止、手入力の配信状況、通知の条件（しきい値と通知対象の項目）、ルール判定の値、ミニバッチの値 |

- 採点した結果は、「判定が正しかったか」ではなく**正解そのもの**（noul なら true/false、severity なら正しい段階）で保存します。あとでしきい値を計算したり、確信度を調整したりするときに使うためです。

---

## 5. データモデル（SQLite）

```sql
-- 監視セッション（配信1回分）
CREATE TABLE sessions (
  id              INTEGER PRIMARY KEY,
  channel_login   TEXT NOT NULL,
  started_at      TEXT NOT NULL,
  ended_at        TEXT,
  game_name       TEXT,
  stream_title    TEXT,
  welcomes_advice TEXT,          -- 'yes' | 'no' | 'unknown'
  spoiler_note    TEXT
);

-- メッセージ
CREATE TABLE messages (
  id                TEXT PRIMARY KEY,  -- Twitch のメッセージ ID
  session_id        INTEGER NOT NULL REFERENCES sessions(id),
  author_pseudo_id  TEXT NOT NULL,     -- HMAC による仮名 ID
  text              TEXT NOT NULL,
  sent_at           TEXT NOT NULL,
  rule_flags        TEXT,              -- JSON 例: ["repeat","url"]
  judge_status      TEXT NOT NULL,     -- 'pending' | 'done' | 'skipped_overload' | 'error'
  notified          INTEGER NOT NULL DEFAULT 0
);

-- Laya の判定結果（メッセージ1件 × 質問1つで1行）
CREATE TABLE judgments (
  message_id   TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  question_id  TEXT NOT NULL,         -- 'abuse' など
  value        TEXT NOT NULL,         -- noul は P(true)、choice は選ばれたキー
  probs        TEXT,                  -- JSON（choice の場合は選択肢ごとの確率）
  confidence   REAL,
  model_ver    TEXT NOT NULL,         -- 例: 'laya-0.3.21/multilingual'
  latency_ms   REAL,
  PRIMARY KEY (message_id, question_id)
);

-- 採点（正解）
CREATE TABLE labels (
  message_id   TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  question_id  TEXT NOT NULL,
  truth        TEXT NOT NULL,         -- 'true'/'false' または severity のキー
  labeled_at   TEXT NOT NULL,
  PRIMARY KEY (message_id, question_id)
);

-- 設定（画面から変更できる値）
CREATE TABLE settings (
  key    TEXT PRIMARY KEY,
  value  TEXT NOT NULL                -- JSON
);
```

---

## 6. API（FastAPI）

| メソッド | パス | 内容 |
|---|---|---|
| GET | `/api/status` | 接続の状態、キューの長さ、直近の判定時間 |
| POST | `/api/monitor/start` | `{channel_login}` を受け取って監視を始める |
| POST | `/api/monitor/stop` | 監視を止める |
| GET/PUT | `/api/context` | 配信状況（自動取得した値と手入力の値） |
| GET | `/api/messages` | 一覧（絞り込み：採点済みかどうか、通知済みかどうか、項目、期間） |
| PUT | `/api/messages/{id}/labels` | 採点を保存する |
| GET | `/api/stats/summary` | 件数や正解率などの集計 |
| GET | `/api/stats/threshold` | しきい値を変えたときのシミュレーション結果 |
| GET/PUT | `/api/settings` | 設定 |
| WS | `/ws/live` | 新しいメッセージと判定結果を配信する（表示名はここでだけ流す） |
| GET | `/` | ビルド済みのダッシュボード（静的ファイル） |

---

## 7. 設定と秘密情報

`.env`（Git には入れない。`.env.example` を用意する）：
```
DISCORD_WEBHOOK_URL=
TWITCH_CLIENT_ID=           # 任意。空なら配信状況の自動取得は無効
TWITCH_CLIENT_SECRET=       # 任意
PSEUDONYM_SALT=             # 空なら初回起動時に自動で作る
HOST=127.0.0.1
PORT=8765
```
- 質問の定義は `config/questions.yaml`、通知条件などの初期値は `config/defaults.yaml` に置きます。

---

## 8. プライバシーと運用上のルール
- 他人の配信のコメントを扱うので、**データは PC の外に出しません**。例外は Discord 通知だけです。
- ダッシュボードは `127.0.0.1` だけで待ち受け、外部には公開しません。
- データベース、ログ、`.env` は `.gitignore` に入れます。
- 採点済みデータを公開したり共有したりしないこと。

---

## 9. 性能と過負荷への対策
- **最初に測ること（M2）**：Surface で、ミニバッチ（1件・4件・8件）ごとに「7項目の判定にかかる時間」を測り、1秒あたり何件さばけるかを求めます。
- **過負荷への対策**
  - 判定キューの上限（初期値200件）を超えたら、古いものから判定を飛ばし、`skipped_overload` として記録します。ルール判定は、飛ばしたメッセージにも必ず行います。
  - ダッシュボードに「過負荷で判定を飛ばした件数」を表示します。
- モデルは起動時に読み込み、判定のたびに読み込み直すことはしません。

---

## 10. Laya に関する既知のリスクと対策

| リスク | 対策 |
|---|---|
| 追加学習なしのモデルでは精度が低い可能性がある（README で明言されている） | 採点データで精度を測ることを中心に据える。自動の対処は行わず、通知だけにする |
| 多言語用モデルの確信度が調整されていない（間違っていても確信度が高く出る） | しきい値は採点データを見て決める。200件以上たまったら、確信度の調整（temperature fitting）を検討する |
| 動作確認で、日本語の文を `domain: code (p=0.997)` と誤判定した | 同じように確信度の高い誤判定が起きる前提で、統計画面の誤通知シミュレーションで監視する |
| `score` 型の位置の偏り（issue #131） | `severity` は `choice` 型にする |
| `noul` 型の名前への偏り（issue #156） | 質問ごとに `labels` を指定できるようにする |
| Surface の処理速度 | ミニバッチ、キューの上限、判定の飛ばしで対応する（§9） |

---

## 11. ディレクトリ構成（案）

```
twitch-moderation/
├─ backend/
│  ├─ app/
│  │  ├─ main.py              # FastAPI の起動、各部の組み立て
│  │  ├─ config.py
│  │  ├─ twitch/irc.py        # 受信
│  │  ├─ twitch/helix.py      # 配信状況の取得
│  │  ├─ rules/               # 連投・URL
│  │  ├─ judge/worker.py      # Laya 判定ワーカー
│  │  ├─ judge/questions.py   # YAML の読み込み
│  │  ├─ decide.py            # 通知するかの判定
│  │  ├─ notify/discord.py
│  │  ├─ db/                  # スキーマ、リポジトリ、保持期限の削除ジョブ
│  │  └─ api/                 # REST と WebSocket
│  ├─ config/questions.yaml
│  ├─ config/defaults.yaml
│  └─ pyproject.toml
├─ frontend/                  # Vite ＋ React ＋ TypeScript
├─ data/                      # SQLite（Git には入れない）
├─ .env.example
└─ README.md
```

---

## 12. マイルストーンと完了条件

| # | 内容 | 完了条件 |
|---|---|---|
| M1 | Twitch の受信 | 指定した公開配信のコメントが、ターミナルに1件ずつ表示される。切断されても自動で再接続する |
| M2 | Laya 判定と SQLite 保存 | 7項目の判定が保存される。Surface で1秒あたりに処理できる件数が測れている |
| M3 | ルール判定と Discord 通知 | 連投・URL の印が付く。§4.5 の条件で Discord に通知が届く |
| M4 | ダッシュボード（ライブと採点） | リアルタイム表示と、項目ごとの採点ができる |
| M5 | 統計・設定・保持期限 | しきい値シミュレーションと設定変更が画面から行える。30日を過ぎた未採点データが消える |
| M6 | 評価 | 200件以上を採点し、項目ごとの正解率と、推奨するしきい値をレポートにまとめる |

---

## 13. 将来の拡張（今回は作らない）
- YouTube Live 対応（YouTube Data API。1日の利用回数の上限に注意）
- 複数配信の同時監視
- 採点データを使った確信度の調整や、追加学習（ファインチューニング）
- 精度が十分だと確認できた場合の、自動タイムアウトの検討

---

## 14. 参考
- Laya: https://github.com/NandhaKishorM/laya （README の Honest limits、issue #131・#156）
- Twitch チャット（IRC）と Helix API の公式ドキュメント 【要確認】
- Discord Webhook の公式ドキュメント 【要確認】
