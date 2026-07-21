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
