# Sonnet 用指示書 — パターン確立済みの実装タスク

対象: Claude Sonnet のセッション。
共通規約・検証ゲート・git 制約は必ずルートの [`CLAUDE.md`](../../CLAUDE.md) に従うこと。
設計判断が重いタスク (アーキテクチャ変更・キャッシュ正当性・ルーティング戦略) は
[`instructions-opus.md`](./instructions-opus.md) の担当 — 着手しない。判断に迷う
設計分岐に当たったら、実装を止めて設計課題として記録する。

## 現状サマリ

長所・短所の詳細は [`instructions-opus.md`](./instructions-opus.md) の
「現状サマリ」を参照 (同じ内容を二重管理しない)。Sonnet セッションで特に守ること:

- 新しい per-client / per-request 構造には必ず上限と eviction (既存例:
  `Metrics.max_tracked_clients`, `RateLimiter.max_buckets`)。
- provider の HTTP は `Provider._client()` を再利用 (per-request で
  `httpx.AsyncClient(...)` を作らない)。
- コミット前に CLAUDE.md の検証ゲートを全部通し、live 検証をコミットメッセージに書く。

## 割当タスク (優先度順)

> **進捗**: S-6〜S-8, S-10〜S-13 は実装・push 済み (下記各項の ✅ 参照)。
> 未着手は S-1〜S-4、および S-9 (O-7 のデフォルト方針決定待ち)。S-5 はメンテナ権限待ち。

### S-1. E2E スイートの v1.2 機能カバー
- `apps/web/e2e/` に spec を追加:
  1. OpenAI 互換エンドポイント: API (port 8090) に直接 `POST /v1/chat/completions`
     (非ストリーム + stream: true) を投げ、`chat.completion` / `chunk` 形状を assert。
  2. Settings ページの「Use it from the OpenAI SDK」セクションが表示され、
     スニペットにベース URL が含まれること。
- **踏襲するパターン**: `apps/web/e2e/helpers/api-server.ts` (per-spec API 起動、
  固定ポート、serial 実行)、既存 3 spec (`chat.spec.ts` など) の構成。
  `playwright.config.ts` は変更不要のはず。

### S-2. `REKAI_CUSTOM_BASE_URL` の registry 統合テスト
- 監査指摘: env → `providers/registry.py` の custom provider 登録経路が未テスト
  (既存テストは `OpenAICompatibleProvider` を直接構築するのみ)。
- registry は import 時初期化なので、テストは `importlib.reload` + monkeypatch.setenv
  か、subprocess で env を立てて確認する形になる。Opus タスク O-1 (DI 化) が入ると
  書き直しになるため、**軽量に** (1-2 テスト)。

### S-3. web devDependencies の npm audit 対応
- `npm audit` が 10 件 (moderate 4 / high 5 / critical 1) を報告 (eslint 8 EOL、旧
  glob 等)。runtime 依存ではなく devDeps だが、公開リポジトリとして解消する。
- 依存更新後は web の全検証 (tsc / lint / vitest / build / E2E) を必ず通す。
  eslint 9 移行が必要なら flat config への移行込みで 1 コミット。

### S-4. 小粒の改善 (各 1 コミット)
- `/metrics` (Prometheus 出力) は現状 per-client 3 系列のみ。retries/cooldowns は
  出力済みなので、必要なら provider 別トークン等を検討 — ただし cardinality に注意
  (client id 系列は `REKAI_MAX_TRACKED_CLIENTS` で有界)。
- `Provider._client()` の timeout は初回構築時に固定される — settings の
  `request_timeout_seconds` を変えて `create_app` し直しても既存クライアントには
  効かない。docstring には記載済み。テストで顕在化したら、timeout 変更時の再構築
  (キーに timeout を含める) を追加。

### S-5. 権限復旧後の後処理 (メンテナが権限を付与したら)
1. `git push origin v1.2.0` (タグは作成済み・ローカルにある)
2. CI コミット (tip に隔離されている `ci: add GitHub Actions workflow...`) を push
3. push できたら GitHub Actions の初回実行を確認し、失敗ジョブがあれば修正
4. GitHub Release (v1.2.0) を CHANGELOG の [1.2.0] 節から作成

### S-6. 価格表・モデル一覧の 2026 更新 (F2 の即値部分)
> ✅ **完了** (`57b4522`): openai/gemini の `list_models()` に o1/o3/gemini-2.5-pro を
> 追加し、「広告する全モデルは価格表にあり同プロバイダへルーティングされる」不変条件を
> `test_providers.py` に追加。
- `pricing.py:27-50` の価格表と各 `list_models()` (`providers/openai.py:180`、
  `providers/gemini.py:200-204`) が相互不整合。openai は o1/o3 系を出さず、gemini は
  `gemini-2.5-pro` を欠く。`gpt-4.1`/`gpt-5`/`o4-mini`/`gemini-2.5-flash` も未収録。
- 不整合の核: `router.py:18-21` の `_MODEL_PREFIX_RULES` は o1/o3 → openai を
  ルーティングするのに、`openai.py:180` の `list_models()` はそれらを一切公開しない。
- 対処: 3 箇所 (pricing + 2 つの list_models) を同時に更新し、router のルールと
  `/v1/models` の公開一覧を揃える。Anthropic の prefix (`claude-*`) は現行 id と
  一致するので触らなくてよい。
- **O-8 (単一情報源化) が入るまでの暫定**。値の出典 (公式価格ページ) をコミットに記載。

### S-7. Admin ページの 404 判定修正 (F4)
> ✅ **完了** (`6599c75`): `errorFromResponse` を typed `ApiError`(`.status` 付き)に
> し、admin ページは `e.status === 404` で分岐(メッセージ文字列比較を廃止)。
- `app/admin/page.tsx:35` が `msg === "Not Found"` という FastAPI 既定 404 本文の
  文字列一致で「admin API 未設定」を判定 → サーバの detail 文言変更で壊れる。
- 対処: `lib/api.ts` の `errorFromResponse` に HTTP ステータスを通し、`=== 404` で
  分岐。既存の `RekAIError`-風パターンがあれば踏襲。

### S-8. チャット UI の a11y パス (F5)
> ✅ **完了** (`ec3a5eb`): 会話領域を `role="log"` `aria-live="polite"` に、エラーを
> `role="alert"` に、メッセージキーを安定 id ベースに。E2E に live region assert 追加。
- `app/page.tsx:317-355`: メッセージコンテナ/ストリーミング中の assistant バブルに
  `aria-live` なし、エラーバナーに `role="alert"` なし、`key={i}` の index キーが
  `regenerate()`/`clearConversation()` でずれる。
- 対処: メッセージ領域に `aria-live="polite"`、エラーに `role="alert"`、キーを
  安定 id ベースに。E2E に軽い a11y assert を足すと尚可。

### S-9. デプロイ manifest の締め付け (F6 の実装部分、O-7 決定後)
- `docker-compose.yml:19` と `deploy/render.yaml:32` の `REKAI_CORS_ORIGINS: "*"` を
  コメント付きで安全化 (例: 具体オリジンのプレースホルダ + 認証キー設定の案内)。
  **O-7 のデフォルト方針が決まってから**着手。

### S-10. Python 非同期クライアント (F7)
> ✅ **完了** (`bfcfd73`): `AsyncRekAIClient` を追加(httpx.AsyncClient、`async for`
> ストリーム)。共通の plumbing をモジュール関数に切り出し sync/async の乖離を防止。
- `packages/python-sdk` は同期 `httpx.Client` のみ (`client.py:103`)。JS SDK は全 async。
- `AsyncRekAIClient` を既存サーフェスのミラーで追加 (`httpx.AsyncClient`)。`stream()` は
  `async for`。テストは既存 `tests/test_client.py` の `httpx.MockTransport` パターンを
  async 版で踏襲。

### S-11. SDK に Idempotency-Key + クライアント側リトライ (F8)
> ✅ **完了** (`40b740c`): 両 SDK に指数バックオフのリトライ(429/5xx/接続エラー、
> Retry-After 尊重)と `idempotency_key`/`idempotencyKey`(リトライ有効時は自動生成)。
- サーバは `Idempotency-Key` を尊重する (`main.py`) が、Python/JS どちらの SDK からも
  送れず、接続エラー/429 のリトライもない。両 SDK に `idempotency_key` 引数と
  簡易リトライ (指数バックオフ) を追加。**S-10 の後、または独立で**可 (ヘッダ透過は
  現行サーバでそのまま有効)。O-5 のサーバ側強化とは独立。

### S-12. ストリーミング tool_calls の一級イベント化 (F9)
> ✅ **完了** (`1d75614`): 両 SDK に `on_tool_calls`/`onToolCalls` コールバックを追加
> (summary イベントの `tool_calls` を単独で受け渡し)。ワイヤ形状は不変・後方互換。
- `service.py` は tool_calls を summary/usage SSE イベント内に同梱し、SDK は
  それを `on_usage`/`onUsage` にのみ渡す (Python docstring `client.py:222-227` にも
  未記載)。SDK に `on_tool_calls`/`onToolCalls` コールバックを足し、README/docstring
  に記載。サーバのフレーム形状は変えない (後方互換)。

### S-13. smoke.sh の jq 化 (F10)
> ✅ **完了** (`df1ba54`): `jq -e` のフィールド単位 assert に置換し、認証系ネガティブ
> ケース(`REKAI_API_KEY` 設定時に未認証 /v1/chat が 401)を追加。jq 前提を明記。
- `scripts/smoke.sh:29-42` は compact JSON の部分文字列 grep (`"status":"ok"` 等) で
  脆い。`jq` のフィールド単位チェックに置換し、認証系ネガティブケース (キー設定時の
  401) を 1 つ追加。`jq` 前提を README/Makefile に明記。
