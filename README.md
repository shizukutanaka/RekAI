# RekAI

**音声またはデスクトップ上で、今見ているものを調べて答えてくれるアシスタント。**

画面（または写真）に写っているものを取り込んで、音声かテキストで質問すると、内容を調べて文章で答え、読み上げることもできます。単一HTMLファイル・外部依存ゼロの Web アプリで、インストールは不要です。PC でもモバイルでも動きます。

> English summary below.

## できること

- **画面を取り込む**: PC では画面共有で今の画面をそのまま撮影。モバイル等では写真を撮る／画像を選ぶ。貼り付け（Ctrl+V）にも対応。
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
- **Share into RekAI**: when installed as a PWA (served over HTTPS), RekAI appears in other apps' share sheets; sharing an image opens RekAI with that image already captured. The hand-off stays on the device (a service worker passes it through a local inbox cache, deleted once captured) — no server is involved.
- **You bring your own AI endpoint**: any OpenAI-compatible API with a vision-capable model. Presets for Pasture (this family's local LLM router), Ollama, LM Studio, and OpenAI.
- **Zero-config path**: the settings dialog can auto-detect a local engine (Pasture / Ollama / LM Studio, in that order) running on your device. Probing is loopback-only, sends no API key and nothing external, and nothing is saved until you confirm the found endpoint. If nothing is found, it shows the likely reasons and how to set each engine up.
- **Privacy**: images and questions go only to the endpoint *you* configure. Settings and history live only in your browser on your device. Nothing is ever sent to the developer or any third party. History stores text only, never images.
- UI in 日本語 / English / Español / 中文 / 한국어.
- Note: the previous RekAI (an AI gateway) was consolidated into [Pasture](https://github.com/shizukutanaka/Pasture); this repository has been rebuilt as the assistant described above.
