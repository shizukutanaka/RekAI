# RekAI

**音声またはデスクトップ上で、今見ているものを調べて答えてくれるアシスタント。**

画面（または写真）に写っているものを取り込んで、音声かテキストで質問すると、内容を調べて文章で答え、読み上げることもできます。単一HTMLファイル・外部依存ゼロの Web アプリで、インストールは不要です。PC でもモバイルでも動きます。

> English summary below.

## できること

- **画面を取り込む**: PC では画面共有で今の画面をそのまま撮影。モバイル等では写真を撮る／画像を選ぶ。貼り付け（Ctrl+V）にも対応。
- **質問する**: 音声入力（日本語対応）またはテキスト入力。画像なしの質問もできます。
- **答える**: 画像と質問を、あなたが設定した AI（Vision 対応 LLM）に送り、回答を表示。読み上げ（停止ボタン付き）もできます。
- **履歴**: 質問と回答はこの端末のブラウザ内にだけ残ります（画像は保存しません）。いつでも消せます。
- **多言語 UI**: 日本語／English／Español／中文／한국어。

## 使い方（3ステップ）

1. **接続先を設定する**（初回のみ）: 右上の「設定」で、使う AI の接続先URL・モデル名・（必要な場合）APIキーを入れます。プリセットから選べます（下記）。
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

## プライバシー

- 画像と質問が送られるのは、**あなたが設定した接続先だけ**です。
- 設定と履歴はこの端末のブラウザ内（localStorage）にのみ保存されます。**作成者（開発者）を含む第三者にデータが送られることは一切ありません**（このアプリ自体に送信機能がありません）。
- 履歴に保存するのは質問と回答のテキストだけです。取り込んだ画像は保存しません。
- 注意（ブラウザの仕様）: 音声入力はブラウザの音声認識を使います。ブラウザによっては（例: Chrome）音声がブラウザ提供元のサーバーで処理されます。気になる場合はテキスト入力をご利用ください。読み上げもブラウザ依存で、一部の音声は外部で合成される場合があります。

## 失敗したとき

接続できない・認証エラー・モデルが見つからない等は、理由が分かるメッセージで表示します（黙って失敗しません）。メッセージの案内に沿って、接続先URL・モデル名・APIキー・接続先が動いているかを確認してください。

## このリポジトリについて

旧 RekAI は AI ルーター／ゲートウェイでしたが、その機能は [Pasture](https://github.com/shizukutanaka/Pasture) へ統合済みです（Anthropic Messages 互換、Gemini 対応、Idempotency-Key 重複排除、`pasture discover`）。このリポジトリは、製品定義にもとづいて**「今見ているものを調べて答えるアシスタント」として作り直した**ものです。

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
- Capture your screen (screen sharing), pick/take a photo, or paste an image; ask by voice (Web Speech API) or text; get a written answer, optionally read aloud.
- **You bring your own AI endpoint**: any OpenAI-compatible API with a vision-capable model. Presets for Pasture (this family's local LLM router), Ollama, LM Studio, and OpenAI.
- **Privacy**: images and questions go only to the endpoint *you* configure. Settings and history live only in your browser on your device. Nothing is ever sent to the developer or any third party. History stores text only, never images.
- UI in 日本語 / English / Español / 中文 / 한국어.
- Note: the previous RekAI (an AI gateway) was consolidated into [Pasture](https://github.com/shizukutanaka/Pasture); this repository has been rebuilt as the assistant described above.
