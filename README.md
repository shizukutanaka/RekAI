# RekAI

**音声またはデスクトップ上で、今見ているものを調べて答えてくれるアシスタント。**

画面（または写真）に写っているものを取り込んで、音声かテキストで質問すると、内容を調べて文章で答え、読み上げることもできます。単一HTMLファイル・外部依存ゼロの Web アプリで、インストールは不要です。PC でもモバイルでも動きます。

**正規の配布形態はデスクトップアプリ（ソフトウェア）です。** 同じ `index.html` を Electron の殻で包んだもので、[Releases](https://github.com/shizukutanaka/RekAI/releases) から入手する形を基準とします（Windows=portable `.exe`／macOS=`.zip`／Linux=`.AppImage`）。単一 HTML 版（ブラウザ・PWA）も引き続き使えます。デスクトップ版は**未署名**のため、初回起動時に OS の警告が出ることがあります（下記「デスクトップ版」）。

> English summary below.

## できること

- **画面を取り込む**: PC では画面共有で今の画面をそのまま撮影。モバイル等では写真を撮る／画像を選ぶ。貼り付け（Ctrl+V）にも対応。
- **送る範囲を選ぶ**: 取り込んだ画像の上をドラッグして、AI に送る部分だけを切り出し。関係ない画面（通知・他のウィンドウ）を送らずに済み、小さな文字も縮小されずに届きます。切り出しは端末内で行います。
- **他のアプリから共有する**: インストール済み（PWA）なら、写真アプリ等の「共有」に RekAI が現れ、選ぶだけでその画像を取り込んで質問を始められます（下記）。
- **質問する**: 音声入力（日本語対応）またはテキスト入力。画像なしの質問もできます。
- **答える**: 画像と質問を、あなたが設定した AI（Vision 対応 LLM）に送り、回答を表示。読み上げ（停止ボタン付き）もできます。
- **履歴**: 質問と回答はこの端末のブラウザ内にだけ残ります（画像は保存しません）。いつでも消せます。
- **多言語 UI**: 日本語／English／Español／中文／한국어。

## 使い方（3ステップ）

1. **接続先を設定する**（初回のみ）: 右上の「設定」を開き、まず「自動で探す」を押してみてください。この端末で動いているローカルAI（Pasture / Ollama / LM Studio）を探し、見つかれば確認してそのまま使えます。見つからない場合は理由と導入手順が表示されます。手動で接続先URL・モデル名・（必要な場合）APIキーを入れることもできます（プリセットあり、下記）。
2. **取り込む**: 「画面を取り込む」か「画像を選ぶ／撮る」で、今見ているものを取り込みます。
3. **質問する**: 音声かテキストで質問して「調べる」。回答が表示され、必要なら読み上げます。

`index.html` をブラウザで開くだけで使えます（画面共有や音声入力を使う場合は、HTTPS か `localhost` で開いてください。ブラウザの仕様です）。

## 接続先の設定

AI の接続先は**利用者自身のもの**を使う設計です。OpenAI 互換 API（`/v1/chat/completions`）を持つサービスなら使えます。

| プリセット | 接続先の例 | 備考 |
|---|---|---|
| Pasture | `http://localhost:8080/v1` | この製品群のローカルLLMルーター。モデル名は Pasture 側の設定に合わせて入力 |
| Ollama | `http://localhost:11434/v1` | モデル例: `llama3.2-vision` |
| LM Studio | `http://localhost:1234/v1` | 画像が読めるモデルを読み込んでおく |
| OpenAI | `https://api.openai.com/v1` | 自分の API キーが必要。モデル例: `gpt-4o-mini` |
| カスタム | 任意 | OpenAI 互換なら何でも |

- **画像が読める（Vision 対応）モデル**が必要です。テキスト専用モデルでは画像の質問に答えられません。
- APIキーはこの端末のブラウザ内にのみ保存され、設定した接続先へのリクエストにだけ使われます。

### 自動で探す（ゼロ設定パス）

設定画面の「自動で探す」は、この端末上のローカルAIを **Pasture → Ollama → LM Studio** の順に探します。

- 探す範囲は**この端末の中（ループバック）だけ**です。外部へは何も送らず、探している間は API キーも送りません。確認するのは各エンジンのモデル一覧（OpenAI互換の `/models`）だけです。
- 見つかると、接続先とモデル名（そのエンジンの既定モデルか、画像が読めそうなモデルを優先して選択）が表示されます。**保存されるのは「この接続先を使う」を押したときだけ**です。
- エンジンは動いているのにモデルが1つも無い場合は、その旨と対処が表示されます。何も見つからない場合は、理由の候補と各エンジンの導入手順が表示され、従来どおり手動設定もできます。
- 注意: このページを HTTPS で開いていると、ブラウザが端末内（`http://localhost`）への確認を止める場合があります。その場合は手動設定をご利用ください。

## 他のアプリから共有する（PWA）

HTTPS（または `localhost`）で配信された RekAI は、ブラウザから「インストール」できます。インストールすると、他のアプリの共有シートに RekAI が受け口として現れます。

- 写真や画像を見ているアプリで「共有」→ RekAI を選ぶと、その画像が取り込まれた状態で RekAI が開きます。共有にテキストが含まれていれば、質問欄にも入ります。
- 受け渡しは端末の中で完結します。Service Worker（`sw.js`）が共有をこの端末のブラウザ内の受信箱に一度だけ置き、アプリが取り込んだら消します。**共有された画像がどこかのサーバーへ送られることはありません**（その後、質問するときに初めて、あなたが設定した接続先へ送られます）。
- 必要なファイルは `manifest.webmanifest`、`sw.js`、アイコン画像です。いずれも任意の付随ファイルで、`index.html` を単体で開く従来の使い方では読み込まれず、アプリ本体（単一HTML・依存ゼロ）はそのままです。`file://` で開いた場合は共有受け口は無効です（OS の共有先には出ません）。

## プライバシー

- 画像と質問が送られるのは、**あなたが設定した接続先だけ**です。
- 他のアプリから共有された画像も、端末の中だけで受け渡しされます（受信箱は取り込み後に削除。共有の受け渡し自体にサーバーは介在しません）。
- 「自動で探す」が確認するのは、この端末の中のローカルAIだけです（ループバックへのモデル一覧の確認のみで、外部への送信はありません）。
- 設定と履歴はこの端末のブラウザ内（localStorage）にのみ保存されます。**作成者（開発者）を含む第三者にデータが送られることは一切ありません**（このアプリ自体に送信機能がありません）。
- 履歴に保存するのは質問と回答のテキストだけです。取り込んだ画像は保存しません。
- 注意（ブラウザの仕様）: 音声入力はブラウザの音声認識を使います。ブラウザによっては（例: Chrome）音声がブラウザ提供元のサーバーで処理されます。気になる場合はテキスト入力をご利用ください。読み上げもブラウザ依存で、一部の音声は外部で合成される場合があります。

## 失敗したとき

接続できない・認証エラー・モデルが見つからない等は、理由が分かるメッセージで表示します（黙って失敗しません）。メッセージの案内に沿って、接続先URL・モデル名・APIキー・接続先が動いているかを確認してください。

## このリポジトリについて

旧 RekAI は AI ルーター／ゲートウェイでしたが、その機能は [Pasture](https://github.com/shizukutanaka/Pasture) へ統合済みです（Anthropic Messages 互換、Gemini 対応、Idempotency-Key 重複排除、`pasture discover`）。このリポジトリは、製品定義にもとづいて**「今見ているものを調べて答えるアシスタント」として作り直した**ものです。

## デスクトップ版

- 殻（`desktop/main.js`）は `index.html` をそのまま読み込むだけで、アプリ本体には手を加えていません。contextIsolation 有効／nodeIntegration 無効／sandbox 有効／preload なしで動きます。
- 新しいウィンドウと `index.html` 以外への遷移は遮断します。http／https のリンクは OS の既定ブラウザで開きます。二重起動はせず、既に開いているウィンドウを前面に出します。
- 権限はメディア関連（マイク／画面共有）のみ許可し、それ以外（位置情報・通知など）は拒否します。
- **画面を取り込む**: OS の画面共有ピッカーが開きます（macOS 15 以降はシステムのピッカー）。ピッカーで約10秒以内に選ばないと「Timeout starting video source」で失敗することがあります。その場合はもう一度押してください。
- **音声入力は使えない場合があります。** ブラウザの音声認識（Web Speech API）は、Chrome では Google のサーバーで処理されますが、Electron にはその仕組みが含まれていないため、デスクトップ版では音声入力がエラーになることがあります。テキスト入力をご利用ください（読み上げは OS の音声で動きます）。
- **他のアプリから共有する（PWA）** はデスクトップ版では使えません（`file://` で読み込むため Service Worker を登録しません）。画像は「画像を選ぶ」か貼り付け（Ctrl+V / ⌘V）で取り込めます。
- **未署名です。** Windows では SmartScreen（「Windows によって PC が保護されました」→「詳細情報」→「実行」）、macOS では Gatekeeper（右クリック→「開く」、または「システム設定 > プライバシーとセキュリティ」から許可）の警告が出ることがあります。macOS では初回の画面取り込み時に「画面収録」の許可も求められます。

```sh
npm ci            # electron / electron-builder は版数固定（package-lock 同梱）
npm start         # 殻を起動
npm run dist:win  # または dist:mac / dist:linux（出力先 dist-desktop/）
```

## 開発

```bash
node tests/run-tests.js   # 単体テスト（依存ゼロ、Node だけで実行）
```

核ロジック（リクエスト組み立て、応答パース、設定検証、多言語辞書、履歴）は `index.html` 内の `REKAI CORE` 区間に切り出してあり、テストがそれを抽出して検証します。セキュリティの自己レビューは [SECURITY.md](./SECURITY.md) を参照。

## ライセンス

MIT — [LICENSE](./LICENSE)

---

## English summary

**RekAI is an assistant that looks at what you are looking at — on your desktop screen or in a photo — and answers your questions about it, by voice or text.**

- Single-file (`index.html`), zero-dependency web app. No install; works on PC and mobile.
- **The desktop app is the canonical distribution**: the same `index.html` wrapped in a minimal Electron shell (`desktop/main.js`; contextIsolation on, nodeIntegration off, sandbox on, no preload; only microphone/screen-capture permissions). Builds: Windows portable `.exe`, macOS `.zip`, Linux `.AppImage`. The builds are **unsigned**, so Windows SmartScreen / macOS Gatekeeper may warn on first launch. In the desktop app, voice input may not work (Electron does not include Chrome's speech recognition backend) and PWA share-target is unavailable; use text input and pick/paste images instead.
- Capture your screen (screen sharing), pick/take a photo, or paste an image; ask by voice (Web Speech API) or text; get a written answer, optionally read aloud.
- **Share into RekAI**: when installed as a PWA (served over HTTPS), RekAI appears in other apps' share sheets; sharing an image opens RekAI with that image already captured. The hand-off stays on the device (a service worker passes it through a local inbox cache, deleted once captured) — no server is involved.
- **You bring your own AI endpoint**: any OpenAI-compatible API with a vision-capable model. Presets for Pasture (this family's local LLM router), Ollama, LM Studio, and OpenAI.
- **Zero-config path**: the settings dialog can auto-detect a local engine (Pasture / Ollama / LM Studio, in that order) running on your device. Probing is loopback-only, sends no API key and nothing external, and nothing is saved until you confirm the found endpoint. If nothing is found, it shows the likely reasons and how to set each engine up.
- **Privacy**: images and questions go only to the endpoint *you* configure. Settings and history live only in your browser on your device. Nothing is ever sent to the developer or any third party. History stores text only, never images.
- UI in 日本語 / English / Español / 中文 / 한국어.
- Note: the previous RekAI (an AI gateway) was consolidated into [Pasture](https://github.com/shizukutanaka/Pasture); this repository has been rebuilt as the assistant described above.
