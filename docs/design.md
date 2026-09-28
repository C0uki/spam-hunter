# Twitch チャット荒らし検知ツール 設計書（第2版）

> 第1版（`docs/design-v1.md`）をもとに、設計レビュー（Q1〜Q15）で決めたことを反映した版です。決定の経緯は §15 の「決定ログ」にまとめています。
> 「【要確認】」が付いている箇所は、実装時に公式ドキュメントや実際の API レスポンスを見て確かめてから作ります。

---

## 0. 第1版からの主な変更

| 項目 | 第1版 | 第2版 |
|---|---|---|
| 主目的 | 通知と評価の両方 | **評価が主**。リアルタイムの通知は第2段階（Q1） |
| Laya の判定のタイミング | 受信と同時に判定し、過負荷なら飛ばす | **セッションが終わってからまとめて判定**。飛ばすことはない（Q8） |
| 採点の対象 | 決まっていない（通知されたものが中心） | **層別抽出**（ランダム層＋P(true) の帯ごとの層）。選ばれた確率を記録し、重み付きで推定する（Q2・Q10） |
| 正解の定義 | 決まっていない | 前後の流れを見て付ける。別に `needs_context`（単体では判断できない）の印を付ける（Q6） |
| 配信の選び方 | 公開配信を適当に | ①ストーリー重視ゲームの初見プレイ／②視聴者の多い配信の2つの枠（Q4） |
| 評価の完了条件 | 200件以上を採点 | 各項目で true が30件以上＋ランダム層から300件以上。4週間で打ち切る（Q5） |
| 保持期限 | 未採点は30日で行ごと削除 | 未採点は30日で**本文と仮名 ID だけを消す**。点数と抽出の情報は残す（Q7） |
| Discord 通知 | Laya の判定による通知 | 第1段階は**ルールの印のリアルタイム通知**と**セッション後のまとめ通知**（Q9） |
| セッションの終わり | 手動 | 手動＋**配信の終了を自動で検知**（Q13） |
| 質問の設定 | 1つ | 名前付きの**variant**を複数持てる。本番は1つに固定し、採点済みのメッセージだけを別の variant で判定し直して比べる（Q14） |

---

## 1. 目的とスコープ

### 目的
Twitch のライブ配信チャットを記録し、Laya（非自己回帰型の判定エンジン）で荒らし・不適切発言を7項目判定します。判定結果の一部を人が採点して、**Laya が日本語の配信チャットでどのくらい当たるか**を、信頼区間つきの数字で確かめます。

リアルタイムで荒らしを知らせる運用は、精度が分かってから載せます（第2段階）。第1段階の部品は、第2段階で差し込み直せる形で作ります。

### 第1段階のスコープ（作るもの）
- Twitch の公開配信チャットの受信と記録（同時に1配信）
- ルールによる判定（連投・URL）と、その Discord へのリアルタイム通知
- 配信状況の取得（Helix API と手入力）と、配信の終了の自動検知
- セッションが終わったあとの Laya による7項目の判定（バッチ処理）
- 採点キューへの層別抽出
- セッション後のまとめを Discord に通知
- SQLite への保存と、保持期限が来たメッセージの匿名化
- ローカルで動く Web ダッシュボード（ライブ・採点・統計・設定）
- 重み付きの評価指標（適合率・再現率、95%信頼区間）としきい値のシミュレーション
- variant どうしの比較（質問の文言、`labels`、PyTorch 版と ONNX 版）

### 非スコープ（第1段階では作らないもの）
- Laya の判定をリアルタイムに行うこと、Laya の判定による Discord 通知（第2段階。§14）
- コメントの自動削除やタイムアウト（モデレーター操作）
- YouTube 対応、複数配信の同時監視
- クラウドへのデプロイ

### 運用の位置づけ
試験運用です。特定の配信者ではなく、公開配信で試します。配信は §4.1 の2つの枠から選びます。

---

## 2. 前提と動作環境

| 項目 | 内容 |
|---|---|
| 実行 PC | Surface Go 2（RAM 8GB、2コア CPU、GPU なし、Windows）。**学校の端末の使用許可は取得済み**（Q12） |
| 開発環境 | クラウド上の Linux で開発とテストを行う。Hugging Face、Twitch の IRC、Discord には接続できることを確認済み。本物の Laya を使うテストもここで実行する |
| 移植性 | Windows と Linux の両方で動くように書く（パスは `pathlib`、起動手順は `py -3.12` と `python3.12` の両方を README に書く） |
| Python | 3.12（venv） |
| Laya | 0.3.21。多言語用モデル（`convaiinnovations/laya` の `multilingual`） |
| Node.js | フロントエンドのビルド時だけ使う。ビルド済みのファイルを Git に入れるので、Surface には不要 |
| 起動するプロセス | FastAPI の Python プロセス1つ。受信・ルール判定・通知・バッチ判定・API・画面の配信をすべてこの中で行う |
| 公開範囲 | `127.0.0.1` でだけ待ち受ける |

---

## 3. 全体の構成

### 監視中（配信が続いている間）

```
 Twitch チャット（IRC over WebSocket、匿名接続）
        │
        ▼
 [受信] ──▶ [ルール判定: 連投・URL] ──▶ [SQLite 保存（judge_status = pending）]
                     │                         │
                     ▼                         ▼
             [Discord 通知（ルール）]     [WebSocket でライブ画面へ]

 [配信状況] ◀── Helix API（5分ごと）＋ 手入力
      └─ 2回続けて「配信していない」→ セッションを自動で終える
```

### セッションが終わったあと（自動で順に動く）

```
 [判定のバッチ処理] ──▶ [層別抽出] ──▶ [まとめを Discord に通知]
   pending を順に判定       採点キューへ
   一時停止・再開できる
```

- 監視中は Laya を動かしません。受信・記録・ルール判定は軽い処理なので、Surface への負荷はほぼありません。
- 判定のバッチ処理は、CPU の重い処理です。イベントループを止めないよう、**専用スレッド1本**で実行します。
- 判定の処理は「メッセージ ID の一覧を受け取り、判定して保存する」関数にまとめます。第2段階では、同じ関数をリアルタイム用のワーカーから呼びます。
- 各部品は「配信1つ分」を単位にしたクラスにまとめます（複数配信への拡張に備えるため）。

---

## 4. 各部の詳細

### 4.1 監視の開始と配信の枠
監視を始めるときに、ダッシュボードで次を入力します。

| 項目 | 内容 |
|---|---|
| チャンネル（ログイン名） | 必須 |
| 配信の枠（`purpose`） | `story_firstplay`（①ストーリー重視ゲームの初見プレイ）／`high_traffic`（②視聴者の多い雑談・対戦ゲーム）／`other` |
| 指示を歓迎しているか | `yes`／`no`／`unknown`。**①の枠では必須**（`unknown` を選べない） |
| ネタバレ注意メモ | 任意。例：「ストーリー初見プレイ、第3章まで」 |
| 配信タイトル | Helix API が使えないときだけ手で入力する（任意） |

- ①の枠は、採点する人が**よく知っているゲーム**の配信を選びます。ネタバレと指示厨の採点を正確にするためです。
- ①の枠と②の枠を、それぞれ数セッションずつ集めます（目安は各5セッション）。

### 4.2 Twitch チャットの受信
- 接続先：`wss://irc-ws.chat.twitch.tv:443`【要確認】
- 匿名ログイン：`NICK justinfan<ランダムな数字>` を送ります（`PASS` は不要か、任意の値）。【要確認】
- `CAP REQ :twitch.tv/tags twitch.tv/commands` を要求し、`JOIN #<ログイン名>` で参加します。
- タグから取る情報【要確認】

  | タグ | 使い道 |
  |---|---|
  | `id` | メッセージ ID（主キー） |
  | `user-id` | 仮名 ID の元（HMAC で変換してから保存） |
  | `display-name` | ライブ画面と Discord 通知だけに使う（保存しない） |
  | `tmi-sent-ts` | 送信時刻 |
  | `reply-parent-msg-id` | 返信先のメッセージ ID（採点画面の前後の流れで使う） |

- `PING` には `PONG` を返します。`RECONNECT` を受け取ったら、つなぎ直します。
- 切断されたら、待ち時間を 1秒 → 2秒 → 4秒 … と延ばしながら（上限60秒）自動で再接続します。
- 受信した順に、セッション内の通し番号（`seq`）を振ります。採点画面で「直前10件」を取り出すときに使います。

### 4.3 配信状況と配信の終了の検知
| 項目 | 取得方法 |
|---|---|
| ゲーム名 | Helix API（`GET https://api.twitch.tv/helix/streams?user_login=...` の `game_name`） |
| 配信タイトル | 同じ API の `title` |
| 指示を歓迎しているか・ネタバレ注意メモ | 手入力（§4.1）。監視中に変更もできる |

- アプリ用アクセストークンは、Client Credentials（`POST https://id.twitch.tv/oauth2/token`、`grant_type=client_credentials`）で取ります。リクエストには `Client-Id` と `Authorization: Bearer <token>` のヘッダーを付けます。【要確認】
- 監視を始めたときと、その後5分ごとに取得します。
- **配信の終了の自動検知**：レスポンスの `data` が空（配信していない）という結果が**2回続いたら**、セッションを自動で終えます（`end_reason = 'offline'`）。
- 配信状況が変わったら（ゲームを切り替えた、手入力を変えた、など）、`session_context` テーブルに新しい行を足します。判定では、**そのメッセージが送られた時点の配信状況**を使います。
- API キーが設定されていなければ、自動取得と自動終了は行わず、手入力と手動の停止だけで動きます。

### 4.4 ルールによる判定（Laya を使わない）
| ルール | 初期値（設定画面で変更できる） |
|---|---|
| 連投 | 同じ投稿者が、同じ内容（正規化後）を **60秒以内に3回以上** 書いたら「連投」にする |
| URL 投稿 | URL を含むコメントに印を付ける |

- 正規化の内容：全角・半角の統一（NFKC）、小文字化、空白の除去、同じ文字が3文字以上並んだ部分を2文字に圧縮（例：「wwwww」→「ww」）
- URL は、`http(s)://`・`www.` で始まるものに加え、`bit.ly/xxx` や `discord.gg/xxx` のような、よく使われるトップレベルドメインで終わるドメインも拾います（全角で書かれたものも NFKC で拾う）
- 投稿者の区別は、監視中のメモリ上でだけ行います。
- ルールの印は、**Discord にリアルタイムで通知**します（§4.9）。

### 4.5 Laya による判定

#### モデルと読み込み
- `laya.load("convaiinnovations/laya", subfolder="multilingual")` で読み込みます。ルーター（Router）は使いません。
- 読み込みは、判定のバッチ処理を始めるときに1回だけ行います。バッチ処理が終わったら解放して、メモリを空けます（Surface の 8GB を監視中に占有しないため）。
- CPU のスレッド数は物理コア数（2）に合わせます（`torch.set_num_threads(2)`）。
- ONNX 版（`ONNXAgent`、INT8 量子化版）も、同じインターフェースで使えるようにします（M2 で速度と精度を比べるため）。

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
配信状況は、そのメッセージが送られた時点のもの（§4.3）を使います。

#### 質問の定義（7項目、1回の推論でまとめて判定）

| ID | 種類 | 内容 |
|---|---|---|
| `abuse` | noul | 暴言・誹謗中傷を含むか |
| `personal_attack` | noul | 配信者や他の視聴者への個人攻撃か |
| `spam_promo` | noul | 宣伝・スパムか |
| `sexual` | noul | 性的・不適切な内容か |
| `spoiler` | noul | 配信中のゲームのネタバレか（`game` と `spoiler_note` を参照） |
| `backseat` | noul | 頼まれていないゲームの指示（指示厨）か（`streamer_welcomes_advice` を参照） |
| `severity` | choice | 荒らしとしての深刻度。`none`（問題なし）／`caution`（注意）／`severe`（悪質） |

- `severity` は `score` 型ではなく `choice` 型にします（多言語用モデルの `score` 型には、最初の段階を選びにくい偏りがあるため。issue #131）。選択肢のキーには、true/false や yes/no のような真偽を表す語を使いません。
- noul 型は、`labels`（例：`{"true": "A", "false": "B"}`）で、モデルに見せる選択肢の名前を変えられます（issue #156 への対策。README で公式にサポートされている）。

#### variant（質問の設定の組）
- `config/questions.yaml` に、名前付きの設定（variant）を複数書けるようにします。variant は「質問の文言（`instructions` と `criteria`）」「`labels`」「バックエンド（PyTorch か ONNX INT8 か）」の組です。
  ```yaml
  primary: v1
  variants:
    v1:
      backend: torch
      questions:
        abuse:
          type: noul
          instructions: "このコメントは暴言や誹謗中傷を含みますか？"
          criteria:
            "false": "普通の感想・質問・雑談で、暴言や誹謗中傷を含まない"
            "true": "罵倒、侮辱、差別的な表現、誹謗中傷を含む"
        # ...
    v1-labelsAB:
      extends: v1
      overrides:
        abuse: { labels: { "true": "A", "false": "B" } }
    v1-onnx:
      extends: v1
      backend: onnx-int8
  ```
- **本番の variant（`primary`）は、評価期間中は固定**します。全メッセージの判定と層別抽出は、本番の variant だけで行います。
- ほかの variant は、**採点済みのメッセージだけ**を判定し直して比べます（§4.11）。

#### 判定のバッチ処理
- セッションが終わると自動で始まります（M3。M2 では `python -m app.cli judge` で手動で始める）。`judge_status = 'pending'` のメッセージを送信時刻の順に、`batch_size` 件ずつ（初期値8、設定で変更できる）`predict_batch(states, questions)` で判定します。
  - `sort_by_length` は使いません。バッチの区切りは自前で決めていて、チャットのコメントは長さがそろっているため、効果がほぼないからです。
- 1バッチごとにコミットします。途中で止めても（一時停止、Ctrl+C、PC の再起動）、`pending` のものから再開できます。
- 判定を始めるときに、セッションの `primary_variant` を決めます。一度決めたら、そのセッションは別の variant では判定しません。
- まとめて判定して失敗したら1件ずつ判定し直し、それでも失敗したメッセージは `judge_status = 'error'` にして、エラーの内容をログに残します。
- 本文が消されたメッセージ（匿名化済み）は判定できないので、`error` として扱います。
- ダッシュボードに、進み具合（判定済みの件数／全体、残りの見込み時間）と一時停止ボタンを表示します（M4）。
- 1件あたりの判定時間（バッチの時間÷件数）を記録します。
- 出力の対応（0.3.21 で確認済み）
  - noul：`answers[q]["noul"]`（P(true)）を `judgments.value` に保存します。
  - choice：`answers[q]["choice"]`（選ばれたキー）を `value` に、`answers[q]["probabilities"]`（選択肢ごとの確率）を `probs` に保存します。
  - 共通：`answers[q]["answer_confidence"]` を保存します。

#### ONNX 版
- Laya 本体の書き出しスクリプトは、多言語用モデル（`subfolder`）を指定できません。そのため、同じ手順の `backend/scripts/export_onnx.py` を用意しました。書き出したファイルは `backend/models/` に置きます（Git には入れない）。
- `ONNXAgent` はスレッド数を指定できないので、読み込んだあとに、スレッド数を指定したセッションに作り直します。
- INT8 版は、PyTorch 版と判定がかなり変わることがあります。そのため、速さだけで選ばず、採点データで比べてから使うかを決めます（§4.11 の variant の比較）。

### 4.6 層別抽出（採点キューへ）
判定のバッチ処理が終わったら、そのセッションのメッセージから採点の候補を抽出します。

#### 層（初期値。`defaults.yaml` と設定画面で変更できる）
| 層 | 母集団 | 抽出する件数 |
|---|---|---|
| `random` | そのセッションで判定済みのメッセージ全体 | 30件 |
| `<項目>:high` | noul の各項目で P(true) ≥ 0.9 | 4件 |
| `<項目>:mid` | 0.7 ≤ P(true) < 0.9 | 3件 |
| `<項目>:low` | 0.5 ≤ P(true) < 0.7 | 3件 |
| `severity:severe` | `severity` の判定が `severe` | 5件 |
| `severity:caution` | `severity` の判定が `caution` | 5件 |

- 各層の中では、**単純無作為抽出**（重複なし）で選びます。母集団が抽出件数より少なければ、全件を選びます。
- 層ごとに「母集団の件数 N」と「抽出した件数 n」を記録します（`sample_draws`）。その層での選ばれた確率は n / N です。
- 同じメッセージが複数の層で選ばれることがあります。そのときの選ばれた確率は、π = 1 − Π(1 − n_s / N_s)（層ごとの抽出を独立とみなした近似）とします。統計では、重み 1/π を使います。
- 1セッションあたりの採点件数は、重複を除いて約100件の見込みです（Q10：週1時間前後）。

#### 抽出したメッセージを失わないために
- 採点キューに入ったメッセージは、30日たつと本文が消えて採点できなくなります（§4.10）。採点されないまま消えると、欠測になって推定が偏ります。
- そのため、**25日を過ぎた未採点のキュー**があれば、ダッシュボードに警告を出します。

### 4.7 採点
- 採点画面は、採点キューを順に表示します（層は画面には出しません。採点が層に引きずられないようにするため）。
- **正解は、前後の流れを見て付けます**。モデレーターとして実際に下す判断を正解とします。
- 前後の流れとして、同じセッションの**直前10件**を薄く表示します。
  - 投稿者は「視聴者A・B…」のように、その画面の中でだけ区別できる形で表示します（仮名 ID から、その画面限りの記号を割り当てる）。
  - 返信であれば、返信先のメッセージを示します。
- 1件ごとに次を入力します。
  - 7項目の正解（noul は true/false、`severity` は正しい段階）
  - `needs_context`：コメント単体では判断できない場合に印を付ける
- 「全部正しい」ボタン：Laya の判定をそのまま正解として保存します（noul は P(true) ≥ 0.5 なら true、`severity` は選ばれたキー）。
- キーボードだけで操作できるようにします（例：`1`〜`7` で項目の true/false を切り替え、`s` で深刻度を切り替え、`c` で `needs_context`、`Enter` で保存して次へ、`a` で全部正しい）。
- 保存するのは「判定が正しかったか」ではなく**正解そのもの**です。

### 4.8 しきい値のポリシー（第1段階ではシミュレーションだけ）
第1版 §4.5 の通知条件は、第1段階では**シミュレーションの入力**として残します。第2段階でリアルタイム通知を入れるときの初期値になります。

1. `severity` の判定結果が `severe` のとき
2. `abuse`・`personal_attack`・`spam_promo`・`sexual` のどれかで、P(true) が **0.90 以上**のとき

- 統計画面で「もしこの条件で通知していたら、通知件数と誤った通知はどうなっていたか」を見られるようにします。
- 多言語用モデルは確信度が調整されていない（README の Calibration の節）ので、しきい値は採点データを見て決めます。

### 4.9 Discord 通知
第1段階では2種類の通知を送ります。

| 種類 | タイミング | 内容 |
|---|---|---|
| ルールの通知 | 監視中、リアルタイム | チャンネル名、投稿者の表示名、コメント本文、当てはまったルール（連投・URL）、時刻 |
| まとめ通知 | セッション後の抽出が終わったとき | チャンネル名、配信の枠、セッションの時間、受信した件数、ルールの印の件数、判定した件数、採点キューに入った件数、しきい値（§4.8）を超えた件数。**コメント本文は含めない** |

- 連投の通知の抑制：同じ投稿者については、セッション中の最初の1回だけすぐに通知します。その後は **5分に1回まで**、件数をまとめて通知します。
- Webhook の URL に POST し、Embed を使って見やすい形にします。
- 送信用のキューを用意し、429 エラーが返ってきたら `retry_after` の時間だけ待ってから送り直します。短い時間に通知が集中したときは、1通にまとめて送ります（1通の Embed は最大10個）。【要確認：レート制限の仕様】
- `DISCORD_WEBHOOK_URL` が設定されていなければ、通知は行いません（ほかの機能はそのまま動く）。

### 4.10 保存と保持期限（SQLite）
- 保存先は SQLite の1ファイル（`data/moderation.db`）です。WAL モードで使います。
- **投稿者の扱い**
  - 表示名とユーザー ID は、**データベースには保存しません**。
  - ユーザー ID は、PC ごとに作った秘密の値（ソルト）を使って HMAC-SHA256 で変換した**仮名 ID**として保存します。ソルトは初回起動時に自動で作り、`data/` に置きます（`.env` の `PSEUDONYM_SALT` が設定されていればそちらを使う）。
  - 表示名は、メモリ上のライブ表示と Discord 通知にだけ使います。
- **保持期限（匿名化）**
  - 送信から **30日たった未採点のメッセージ**は、`text` と `author_pseudo_id` を NULL にし、`scrubbed_at` を記録します。
  - 残すもの：メッセージ ID、セッション、時刻、通し番号、返信先 ID、ルールの印、判定結果（点数）、抽出の情報。重み付きの統計の分母として必要だからです。
  - 採点済みのメッセージと、その**直前10件**（同じセッションのもの）は、本文を残します（`kept_as_context = 1`）。
  - 匿名化のジョブは、起動したときと、その後24時間ごとに実行します。

### 4.11 評価指標と統計
- **重み付きの推定**：採点済みのメッセージを、重み 1/π（§4.6）で重み付けして推定します。項目 q、しきい値 t について：
  - 適合率 = Σ w·[s ≥ t]·y ／ Σ w·[s ≥ t]
  - 再現率 = Σ w·[s ≥ t]·y ／ Σ w·y
  - （s は Laya の P(true)、y は正解（true なら1）、w は重み）
- **95%信頼区間**：層ごとに復元抽出するブートストラップ法（初期値1,000回）で出します。件数がごく少ない層しかない場合は、ウィルソンの区間を目安として併記します。
- **2通りの集計**：「全体」と「`needs_context` が付いていないものだけ」の両方を出します。外れの原因が「モデルが弱い」のか「入力の情報が足りない」のかを切り分けるためです。
- **項目ごとの進み具合**：true と採点された件数（目標30件）、ランダム層の採点件数（目標300件）、評価を始めてからの日数（打ち切りは4週間）。
- **しきい値のシミュレーション**：t を動かしたときの、推定される通知件数（1時間あたり）、適合率、再現率。
- **variant の比較**：採点済みのメッセージについて、variant ごとの指標を並べて表示します。重みは本番の variant で決まった π をそのまま使います（選ばれた確率が分かっていれば、別の variant の点数でも推定できるため）。
- **性能**：判定時間の p50 と p95、バッチ処理1回あたりの所要時間。
- `severity` は、段階ごとの適合率と再現率、および混同行列を出します。

### 4.12 ダッシュボード（Vite ＋ React ＋ TypeScript）

| 画面 | 機能 |
|---|---|
| **ライブ** | 受信したコメントとルールの印をリアルタイムで表示します（WebSocket）。監視の状態、配信状況、バッチ処理の進み具合 |
| **採点** | 採点キューを順に表示し、前後の流れを見ながら、7項目の正解と `needs_context` を付けます。キーボードだけで操作できます。25日を過ぎた未採点のキューには警告を出します |
| **統計** | §4.11 のすべて |
| **設定** | 監視の開始と停止（チャンネル、配信の枠、手入力の配信状況）、ルールの値、通知の設定、抽出件数、バッチサイズ、しきい値のポリシー |

- API の型は、FastAPI が出力する OpenAPI の定義から TypeScript の型を自動で作ります。
- ビルド済みのファイル（`frontend/dist`）は Git に入れ、FastAPI から配信します。

---

## 5. データモデル（SQLite）

```sql
-- 監視セッション（配信1回分）
CREATE TABLE sessions (
  id              INTEGER PRIMARY KEY,
  channel_login   TEXT NOT NULL,
  purpose         TEXT NOT NULL,          -- 'story_firstplay' | 'high_traffic' | 'other'
  started_at      TEXT NOT NULL,
  ended_at        TEXT,
  end_reason      TEXT,                   -- 'manual' | 'offline' | 'error'
  pipeline_status TEXT NOT NULL,          -- 'recording' | 'ended' | 'judging' | 'paused' | 'judged' | 'sampling' | 'done' | 'error'
  primary_variant TEXT                    -- このセッションの判定・抽出に使った variant（判定を始めるときに入れる）
);

-- 配信状況の履歴（変わるたびに1行足す）
CREATE TABLE session_context (
  id              INTEGER PRIMARY KEY,
  session_id      INTEGER NOT NULL REFERENCES sessions(id),
  valid_from      TEXT NOT NULL,
  game_name       TEXT,
  stream_title    TEXT,
  welcomes_advice TEXT NOT NULL,          -- 'yes' | 'no' | 'unknown'
  spoiler_note    TEXT,
  source          TEXT NOT NULL           -- 'helix' | 'manual'
);

-- メッセージ
CREATE TABLE messages (
  id                TEXT PRIMARY KEY,     -- Twitch のメッセージ ID
  session_id        INTEGER NOT NULL REFERENCES sessions(id),
  seq               INTEGER NOT NULL,     -- セッション内の受信順
  author_pseudo_id  TEXT,                 -- HMAC による仮名 ID（匿名化で NULL）
  text              TEXT,                 -- 本文（匿名化で NULL）
  sent_at           TEXT NOT NULL,
  reply_parent_id   TEXT,
  rule_flags        TEXT,                 -- JSON 例: ["repeat","url"]
  judge_status      TEXT NOT NULL,        -- 'pending' | 'done' | 'error'
  rule_notified     INTEGER NOT NULL DEFAULT 0,
  kept_as_context   INTEGER NOT NULL DEFAULT 0,
  scrubbed_at       TEXT,
  UNIQUE (session_id, seq)
);

-- Laya の判定結果（メッセージ × variant × 質問 で1行）
CREATE TABLE judgments (
  message_id        TEXT NOT NULL REFERENCES messages(id),
  variant           TEXT NOT NULL,
  question_id       TEXT NOT NULL,
  value             TEXT NOT NULL,        -- noul は P(true)、choice は選ばれたキー
  probs             TEXT,                 -- JSON（choice の場合は選択肢ごとの確率）
  answer_confidence REAL,
  model_ver         TEXT NOT NULL,        -- 例: 'laya-0.3.21/multilingual/torch'
  latency_ms        REAL,
  judged_at         TEXT NOT NULL,
  PRIMARY KEY (message_id, variant, question_id)
);

-- 層ごとの抽出の記録（推定の分母）
CREATE TABLE sample_draws (
  id              INTEGER PRIMARY KEY,
  session_id      INTEGER NOT NULL REFERENCES sessions(id),
  variant         TEXT NOT NULL,          -- 抽出に使った点数の variant（= primary）
  stratum         TEXT NOT NULL,          -- 'random' | 'abuse:high' | 'severity:severe' など
  population_size INTEGER NOT NULL,       -- N
  draw_size       INTEGER NOT NULL,       -- n
  drawn_at        TEXT NOT NULL,
  UNIQUE (session_id, variant, stratum)
);

-- 採点キュー（どの層から選ばれたか）
CREATE TABLE label_queue (
  message_id      TEXT NOT NULL REFERENCES messages(id),
  draw_id         INTEGER NOT NULL REFERENCES sample_draws(id),
  PRIMARY KEY (message_id, draw_id)
);

-- 採点（正解）
CREATE TABLE labels (
  message_id   TEXT NOT NULL REFERENCES messages(id),
  question_id  TEXT NOT NULL,
  truth        TEXT NOT NULL,             -- 'true'/'false' または severity のキー
  labeled_at   TEXT NOT NULL,
  PRIMARY KEY (message_id, question_id)
);

-- 採点のメッセージ単位の情報
CREATE TABLE label_meta (
  message_id     TEXT PRIMARY KEY REFERENCES messages(id),
  needs_context  INTEGER NOT NULL,        -- 1: 単体では判断できない
  labeled_at     TEXT NOT NULL
);

-- 設定（画面から変更できる値）
CREATE TABLE settings (
  key    TEXT PRIMARY KEY,
  value  TEXT NOT NULL                    -- JSON
);
```

- `pipeline_status` の流れ：`recording`（監視中）→ `ended`（判定待ち）→ `judging`（判定中）→ `judged`（判定済み・抽出待ち）→ `sampling` → `done`。判定を途中で止めたら `paused` になり、次の判定で続きから再開します。判定のバッチ処理は `ended`・`judging`・`paused` のセッションを拾います（`judging` のままなのは、判定中にプロセスが落ちた場合）。
- 起動したときに、前回の異常終了で開いたままのセッションがあれば、`end_reason = 'error'` で閉じます（終了時刻は最後のメッセージの時刻）。
- メッセージの重複（再接続したときに同じメッセージ ID が届くなど）は無視します。通し番号（`seq`）がぶつかった場合はエラーにします。
- 選ばれた確率 π は保存せず、`label_queue` と `sample_draws` から計算します（層の定義を変えても、過去の分を正しく扱えるようにするため）。

---

## 6. API（FastAPI）

| メソッド | パス | 内容 |
|---|---|---|
| GET | `/api/status` | 接続の状態、監視中のセッション、バッチ処理の進み具合 |
| POST | `/api/monitor/start` | `{channel_login, purpose, welcomes_advice, spoiler_note, stream_title?}` を受け取って監視を始める |
| POST | `/api/monitor/stop` | 監視を止める（→ バッチ処理が自動で始まる） |
| POST | `/api/pipeline/{session_id}/pause` ・ `/resume` | バッチ処理の一時停止と再開 |
| GET/PUT | `/api/context` | 監視中の配信状況（自動取得した値と手入力の値） |
| GET | `/api/sessions` | セッションの一覧 |
| GET | `/api/label-queue` | 未採点のキュー（次の1件と前後の流れを含む） |
| GET | `/api/messages` | 一覧（絞り込み：セッション、採点済みかどうか、ルールの印、期間） |
| PUT | `/api/messages/{id}/labels` | 採点（7項目の正解と `needs_context`）を保存する |
| POST | `/api/variants/{name}/rejudge` | 採点済みのメッセージを、指定した variant で判定し直す |
| GET | `/api/stats/summary` | 重み付きの指標と信頼区間、進み具合（`?variant=`、`?exclude_needs_context=`） |
| GET | `/api/stats/threshold` | しきい値のシミュレーション |
| GET | `/api/stats/variants` | variant どうしの比較 |
| GET/PUT | `/api/settings` | 設定 |
| WS | `/ws/live` | 新しいメッセージとルールの印、バッチ処理の進み具合を配信する（表示名はここでだけ流す） |
| GET | `/` | ビルド済みのダッシュボード（静的ファイル） |

---

## 7. 設定と秘密情報

`.env`（Git には入れない。`.env.example` を用意する）：
```
DISCORD_WEBHOOK_URL=        # 任意。空なら通知しない
TWITCH_CLIENT_ID=           # 任意。空なら配信状況の自動取得と自動終了は無効
TWITCH_CLIENT_SECRET=       # 任意
PSEUDONYM_SALT=             # 任意。空なら data/ に自動で作る
HOST=127.0.0.1
PORT=8765
```
- 質問の定義と variant は `config/questions.yaml`、抽出件数・ルールの値・しきい値のポリシーなどの初期値は `config/defaults.yaml` に置きます。画面から変えた値は `settings` テーブルに保存し、`defaults.yaml` より優先します。

---

## 8. プライバシーと運用上のルール
- 他人の配信のコメントを扱うので、**データは PC の外に出しません**。例外は Discord 通知だけです（ルールの通知には本文を含む。まとめ通知には本文を含まない）。
- ダッシュボードは `127.0.0.1` だけで待ち受け、外部には公開しません。
- データベース、ソルト、ログ、`.env` は `.gitignore` に入れます。
- 未採点のメッセージの本文は30日で消します（§4.10）。
- 採点済みのデータを公開したり共有したりしないこと。
- 開発環境（クラウド）でのテストには、実際の配信から集めたデータを持ち込みません。テストには自作のサンプルを使います。

---

## 9. 性能
- **最初に測ること（M2）**：Surface で、バッチサイズ（1件・4件・8件）× バックエンド（PyTorch 版と ONNX INT8 版）ごとに、「7項目の判定にかかる時間」を測ります。README によると、CPU ではバッチサイズを増やしても速くなるとは限りません（`sort_by_length` は長さがばらつくときに効く）。そのため、実測で初期値を決めます。
- 見積もり：1件あたり0.3〜0.5秒とすると、1時間に1万件流れる配信の判定には1〜1.5時間かかります。
- 判定は監視が終わってから行うので、監視中に取りこぼすことはありません。長すぎる配信には、一時停止と再開で対応します。
- ONNX 版は精度が変わる可能性があるので、`model_ver` と variant で区別し、採点データで比べてから使うかを決めます。

---

## 10. Laya に関する既知のリスクと対策

| リスク | 対策 |
|---|---|
| 追加学習なしのモデルでは精度が低い可能性がある | 評価を主目的にし、層別抽出と重み付きの推定で精度を測る。自動の対処は行わない |
| 多言語用モデルの確信度が調整されていない | しきい値は採点データを見て決める。十分な件数がたまったら、確信度の調整（temperature fitting）を第2段階で検討する |
| 日本語の文を、確信度が高いまま誤判定することがある | 信頼区間つきの適合率と、誤った通知のシミュレーションで監視する |
| `score` 型の位置の偏り（issue #131） | `severity` は `choice` 型にする |
| `noul` 型の名前への偏り（issue #156） | `labels` を変えた variant を用意し、採点データで比べる |
| コメント単体では判断できないものが多い | `needs_context` の印と2通りの集計で切り分ける。多ければ、第2段階で直前のチャットを `state` に入れる |
| Surface の処理速度 | 監視後のバッチ処理、一時停止と再開、ONNX 版の検討 |

---

## 11. ディレクトリ構成

```
spam-hunter/
├─ backend/
│  ├─ app/
│  │  ├─ main.py              # FastAPI の起動、各部の組み立て
│  │  ├─ config.py
│  │  ├─ twitch/irc.py        # 受信
│  │  ├─ twitch/helix.py      # 配信状況の取得、配信の終了の検知
│  │  ├─ rules/               # 連投・URL
│  │  ├─ judge/               # Laya の読み込み、バッチ判定、variant の読み込み
│  │  ├─ sampling/            # 層別抽出
│  │  ├─ pipeline.py          # セッション後の流れ（判定 → 抽出 → まとめ通知）
│  │  ├─ stats/               # 重み付きの推定、ブートストラップ、しきい値のシミュレーション
│  │  ├─ notify/discord.py
│  │  ├─ db/                  # スキーマ、マイグレーション、リポジトリ、匿名化ジョブ
│  │  └─ api/                 # REST と WebSocket
│  ├─ config/questions.yaml
│  ├─ config/defaults.yaml
│  ├─ scripts/bench.py        # M2 の性能測定
│  ├─ tests/
│  └─ pyproject.toml
├─ frontend/                  # Vite ＋ React ＋ TypeScript（dist は Git に入れる）
├─ docs/
│  ├─ design.md               # この文書
│  └─ design-v1.md
├─ data/                      # SQLite とソルト（Git には入れない）
├─ .env.example
└─ README.md
```

- テスト：普段は偽の判定器を使って pytest で確かめます。本物の Laya を使うテストは「遅いテスト」（`@pytest.mark.slow`）として分けます。

---

## 12. マイルストーンと完了条件

| # | 内容 | 完了条件 |
|---|---|---|
| M1 | 受信・記録・ルール判定（ターミナルで動く） | 匿名の IRC 接続でコメントが SQLite に保存される（仮名 ID、通し番号、返信先 ID を含む）。連投と URL の印が付く。切断されても自動で再接続する |
| M2 | Laya のバッチ判定と性能測定 | `questions.yaml` の variant で7項目を判定し、途中から再開できる。測定用のスクリプトで、バッチサイズ（1・4・8）× PyTorch 版と ONNX INT8 版の処理速度が出る |
| M3 | セッションの流れと通知 | Helix API による配信状況の取得と自動終了。終了すると「判定 → 層別抽出 → まとめ通知」が自動で動く。ルールの印はリアルタイムで Discord に通知され、送りすぎは抑えられる |
| M4 | ダッシュボード（ライブ・採点・設定） | 前後の流れを見ながら7項目と `needs_context` を採点でき、キーボードだけで操作できる |
| M5 | 統計と保持期限 | 重み付きの適合率・再現率（95%信頼区間つき）、しきい値のシミュレーション、項目ごとの進み具合、variant どうしの比較。30日たったメッセージが匿名化される |
| M6 | 評価 | 目標（各項目で true が30件以上、ランダム層から合計300件以上）に届くか、4週間たつまで集め、レポートにまとめる。届かなかった項目は「件数不足で判断保留」と明記する |

- マイルストーンごとに PR を1つ作ります。

---

## 13. 評価の運用手順（M6）
1. ①の枠と②の枠の配信を、それぞれ5セッション程度監視する（4週間以内）。
2. セッションが終わるたびに、採点キュー（約100件）を採点する（週1時間前後）。
3. 統計画面の進み具合を見て、足りない項目があれば、その項目の抽出件数を増やす。
4. 途中で、`labels` や文言を変えた variant を採点済みのデータで判定し直して比べる（本番の variant は変えない）。
5. 4週間たったら（または目標に届いたら）、次の内容をレポートにまとめる。
   - 項目ごとの適合率と再現率（信頼区間つき）
   - 全体と `needs_context` を除いたものの比較
   - 推奨するしきい値
   - variant の比較
   - 第2段階に進むかどうかの判断

---

## 14. 第2段階以降（今回は作らない）
- Laya の判定をリアルタイムに行う（判定の関数をリアルタイム用のワーカーから呼ぶ。過負荷のときは飛ばして、セッション後に後追いで判定する）
- Laya の判定による Discord 通知（§4.8 のポリシーを、評価で決めたしきい値で使う）
- 採点データを使った確信度の調整（temperature fitting）や、追加学習
- 直前のチャットを `state` に入れる
- YouTube Live 対応、複数配信の同時監視
- 精度が十分だと確認できた場合の、自動タイムアウトの検討

---

## 15. 決定ログ（設計レビュー）

| # | 質問 | 決定 |
|---|---|---|
| Q1 | 主目的は評価か運用か | **評価が主**。リアルタイム通知は後から |
| Q2 | 採点するメッセージの選び方 | **層別抽出**（ランダム層＋点数の高い層、選ばれた確率を記録して重み付きで推定） |
| Q3 | 評価する項目 | **7項目すべて** |
| Q4 | 評価に使う配信の選び方 | **2つの枠**（①ストーリー重視ゲームの初見プレイ／②視聴者の多い配信）。①では指示を歓迎しているかの入力を必須に |
| Q5 | 評価の完了条件 | **目標**（各項目で true が30件以上＋ランダム層300件以上）**＋打ち切り**（4週間）。信頼区間を付ける |
| Q6 | 正解の定義 | **前後の流れを見て付ける＋`needs_context` の印**。統計は2通り |
| Q7 | 30日たった未採点のメッセージ | **本文と仮名 ID だけを消す**。採点済みの直前10件は残す。抽出はセッション終了時 |
| Q8 | Laya の判定のタイミング | **セッション後にまとめて判定**。途中から再開できる。ONNX 版も測る |
| Q9 | 第1段階の Discord 通知 | **ルールの印のリアルタイム通知＋セッション後のまとめ通知** |
| Q10 | 採点に使える時間 | **週1時間前後**（1セッションあたり約100件）。点数の高い層は P(true) の帯ごとに抽出 |
| Q11 | ダッシュボードの作り方 | **Vite ＋ React ＋ TypeScript**（型は OpenAPI から自動で作る） |
| Q12 | Surface の使用許可 | **取得済み** |
| Q13 | セッションの終わりの決め方 | **手動＋配信の終了を自動で検知**（2回続けて配信していなければ終了） |
| Q14 | 質問の文言と `labels` の比べ方 | **本番の variant は固定し、採点済みのメッセージだけを別の variant で判定し直して比べる** |
| Q15 | 進め方 | **設計書の第2版を先に確認**してから M1 へ。マイルストーンごとに PR を1つ |
| Q16 | PR を作るための `main` ブランチ | ユーザーが GitHub 上で作成（設計書はそのまま `main` に入った） |
| Q17 | 第2版のまま M1 に進むか | **このまま進む**（細かい点は各マイルストーンの PR で見直す） |
| Q18 | Surface での M1 の確認と M2 の順番 | **並行して進める** |
| Q19 | 本番の variant（v1）の入力に配信状況を含めるか | **全項目に含める（設計どおり）**。自作の6件では、含めると個人攻撃・指示厨・ネタバレの確率が一様に高く出る傾向があったが、評価の採点データで確かめる |
| Q20 | 判定が終わらないほどメッセージが多いとき | **Surface で実測してから決める**。候補は、1セッションで判定する件数に上限を設け、ランダムに選んだ分だけ判定する方法（選ばれた確率を重みに掛けるので、推定は偏らない） |
| Q21 | Surface 以外に使える PC | Surface より遅い PC しかないので、**判定は Surface で行う**。GPU サーバーを借りるのは、§8 の見直しが必要になるため、最後の手段とする |

---

## 16. 参考
- Laya: https://github.com/NandhaKishorM/laya （README の Honest limits、Calibration、Batch Mode、issue #131・#156）
- Twitch チャット（IRC）と Helix API の公式ドキュメント 【要確認】
- Discord Webhook の公式ドキュメント 【要確認】
