# Opus 用指示書 — 設計判断が重いタスク

対象: Claude Opus (またはそれ以上のモデル) のセッション。
共通規約・検証ゲート・git 制約は必ずルートの [`CLAUDE.md`](../../CLAUDE.md) に従うこと。
Sonnet 向けの実装タスクは [`instructions-sonnet.md`](./instructions-sonnet.md)。

## 現状サマリ (v1.2.0 時点)

### 長所 — 壊さないこと
- **SSRF 面なし**: provider は固定レジストリのキーのみ。クライアントが URL を注入する
  経路はない (`providers/registry.py`, `router.py`)。この性質を新機能で破らない。
- **定数時間キー比較 + 非可逆 client id** (`auth.py`)。
- **有界データ構造の規律**: rate limiter (`max_buckets`)、metrics
  (`REKAI_MAX_TRACKED_CLIENTS`)、semantic cache (`deque(maxlen)`)。新しい
  per-client / per-request 構造には必ず上限と eviction を付ける。
- **OpenAI 互換層の品質**: 実 OpenAI SDK での E2E (`tests/test_openai_sdk_e2e.py`)
  が回帰ゲート。互換性を落とす変更はこのテストで検出される。
- **486+ テスト、ruff/mypy クリーン、TODO ゼロ**の状態を維持する。
- **保存前レダクション** (`service._redact`): 応答キャッシュ・セマンティック
  キャッシュ・idempotency ストアの**いずれよりも前**にスクラブする。文書
  (`docs/architecture.md`) の約束はこれ。後から edge で消す形に戻さないこと。
- **テナント境界**: idempotency キー・セマンティックキャッシュのバケット・
  `/v1/usage` の `usage_by_client`・`/metrics` の `rekai_client_*` は
  すべて呼び出し元スコープ。共有ゲートウェイである以上ここは本質。
- **有界性の追加分**: `REKAI_MAX_CONCURRENT_REQUESTS` は**占有**を、レート
  リミッタは**到着**を数える。別物なので両方要る。ヒストグラムの系列数も上限つき。
- **tracing.py の W3C 実装は正しく依存ゼロ** (`ContextVar` 隔離、リクエスト外では
  ヘッダ省略、全ゼロ trace/span id の拒否 `tracing.py:63`)。OTel SDK を将来入れる
  場合もこの性質を保つ。
- **retry.py は Retry-After を `max_delay` で上限化** (`retry.py:79-85`) — 敵対的/
  誤設定 upstream が任意秒数ワーカーを眠らせられない。カスケードルーティング (O-3)
  実装時もこの防御を迂回しないこと。

### 短所 — 既知の構造的制約
1. **プロバイダ層に `create_app(settings)` が届かない**: providers は module-level
   `get_settings()` (env 固定・`@lru_cache`) を直接読む。`registry.py` は import 時に
   設定を読んで custom provider を登録する。テストで env 以外の設定を providers に
   注入できない。**未解決 (O-1)**。
2. **セマンティックキャッシュが単一閾値 + 線形走査** (`semantic_cache.py`)。
   正しさの欠陥 (バケット不足・テナント越え・TTL なし・上限未配線・埋め込み
   コスト未計上) は解消済みだが、**閾値設計と走査コストは未解決 (O-2)**。
3. **ガードレールがヒューリスティックのみ** (`guardrails.py`)。パターンは実測で
   締めた (良性 17 件中 0 誤検知 / 攻撃 21 件中 0 見逃し)、既定は `flag`。
   ただし**言い換え耐性はない**という性質自体は変わらない。分類器導入は未着手。
4. **Anthropic/Ollama は response_format 非対応** (debug ログのみ、文書化済み)。
5. **streaming に Idempotency-Key なし** (意図的・文書化済み — 変更するなら設計から)。
   同様に**ストリームには出力レダクションもかからない** (チャンクをまたぐパターンを
   バッファなしで消せないため。文書化済みの既知ギャップ)。
6. ~~**Idempotency-Key がボディに紐付かない**~~ → **解消** (O-5)。さらに保存キーに
   client_id を混ぜてテナントスコープ化済み。
7. ~~**metrics スナップショットが単一 Redis キーで last-writer-wins**~~ → **解消** (O-6)。
8. **セキュアでないデフォルト**: `cors_origins="*"` + `api_keys=""` (認証オフ) が
   config・docker-compose.yml・deploy/render.yaml すべてに。ワンクリックデプロイが
   全オリジン開放。**未解決 (O-7)**。なお「どの provider に到達できるか」だけは
   `REKAI_ALLOWED_PROVIDERS` で運用者が絞れるようになった (既定は無制限のまま)。
9. **並行数上限とレートリミッタはプロセスローカル** (Redis 設定時のレート
   リミッタを除く)。N ワーカーなら実効上限は N 倍。文書化済み・許容だが、
   厳密な全体上限が要るなら共有カウンタの設計が必要。
10. **`/health` は能動的な疎通確認をしない**。無認証エンドポイントから全
    provider を叩けるようにするのはリクエスト増幅器になるため意図的。
    報告できるのは RekAI が既に持っている状態 (cooldown/circuit breaker) のみ。

## 割当タスク (優先度順)

### O-1. プロバイダ層の設定 DI 化 (短所 1 の解消)
- 目標: `create_app(settings)` の Settings がプロバイダにも届く構造にする。
  案: registry を `create_app` 内で構築してアプリ state に持たせる/providers が
  settings を引数で受ける。**互換性制約**: module-level `metrics`/`cooldowns` 等の
  シングルトンパターンと一貫させること。`registry.py` の import 時初期化を排除。
- 回帰リスク大: 全 362 テストと E2E が緩衝材。`register_provider()` を使うテストが
  多数あるため、レジストリの形を変えるならテスト移行計画込みで。

### O-2. セマンティックキャッシュ three-zone 信頼帯 (短所 2)
- 単一閾値 → 「確実ヒット (>= high) / 不確実 (mid: 検証してから返す or ミス扱い) /
  ミス」の 3 ゾーン。検証は追加レイテンシ (+1 呼び出し) を伴うため、デフォルト off の
  opt-in 設定として設計 (`REKAI_SEMANTIC_CACHE_VERIFY_*`)。誤ヒットは正しさを直接
  壊すので、閾値のデフォルトは保守的に。バケット内線形走査の改善 (バケット索引化) も
  この際に検討。
- 併せて `docs/architecture.md` の Semantic cache 節を更新。

### O-3. コスト×品質カスケードルーティング
- 既存 `fallbacks` 機構 (service.py `_build_attempts`) を土台に、「安い先行モデルで
  試し、低信頼応答のときだけ上位モデルへエスカレーション」を opt-in で追加。
  信頼判定の設計 (長さ/logprobs は取れないので、応答の自己申告 or 分類器) が本体。
  RouteLLM 系の知見を参照。**破壊的変更にしない**: 既存 fallbacks の意味は 5xx/429
  時のみ、と明確に区別する。

### O-4. プロンプトキャッシング・パススルー
> ✅ **完了**: `ChatRequest`/`ChatMessage` に `cache_control` を追加(トップレベルは
> 最終ブロック、メッセージ単位はそのメッセージに付与。文字列 content は text ブロックへ
> 昇格)。`Usage` に `cache_read_tokens`/`cache_write_tokens`(既定 0 で後方互換、
> `prompt_tokens` の内訳)を追加し、Anthropic の `cache_read_input_tokens`/
> `cache_creation_input_tokens`(非ストリーム+ストリーム)と OpenAI の
> `prompt_tokens_details.cached_tokens` から取得。`estimate_cost` は読出 0.1倍・
> 書込 1.25倍で計上し二重計上なし。`cache_key` にも含めて衝突回避。
- Anthropic `cache_control` / OpenAI automatic caching の透過。ChatRequest への
  フィールド追加はキャッシュキー (`cache.py::cache_key`) への影響を必ず設計に含める
  (response_format 追加時の前例: キーに含めないと衝突、含めると一回限りの全キャッシュ
  無効化)。

### O-5. Idempotency セマンティクスの強化 (短所 6)
> ✅ **完了**: 保存レコードにボディの sha256 指紋を含め、同キー+別ボディ→422、
> in-progress センチネル(`cache.add`=Redis `SET NX`/メモリはイベントループ原子性)で
> 同時実行→409、エラー時は `release` でセンチネル解放。全キャッシュ操作は fail-open。
> `/v1/chat`(+ OpenAI 互換)と `/v1/embeddings` の両方。`CacheBackend` に `add`/`delete`
> を追加。ライブ検証(replay/422)+ claim/complete/release のモジュールテスト済み。
- 保存値にリクエストボディの指紋 (sha256) を含め、同キー+別ボディは 422
  (Stripe 方式)。in-progress センチネルで同時実行を合流 (待機して同じ応答) か
  409 を返す。**設計判断**: キャッシュバックエンドの原子性 — Redis なら `SET NX`、
  メモリなら別途ロック。fail-open 方針 (Redis 障害時) との整合も。`idempotency.py`
  と `main.py:251-267` / embeddings 側の両方。

### O-6. マルチレプリカ metrics 集計 (短所 7)
> ✅ **完了**: per-instance キー (`rekai:metrics:snapshot:<instance-id>`) に変更。
> `load()` は自インスタンスのキーのみをベースラインに、`/v1/usage` は自 live + 他
> レプリカの永続スナップショット (`load_others`) を `merge_snapshots` で合算。`/metrics`
> は per-instance のまま (Prometheus が集計するため二重計上を回避)。instance id は
> `REKAI_INSTANCE_ID` かプロセス起動時の乱数。純関数 merge + エンドポイント集計をテスト。
- 単一キー上書きを廃し、per-instance キー (`rekai:metrics:snapshot:<instance-id>`)
  + 読み取り時合算、または Redis アトミックインクリメントへ。`metrics_store.py`
  の load/save と `Metrics.seed/snapshot` の契約変更を伴う。instance id の採番
  (env or 起動時 uuid) も設計に含める。

### O-7. セキュアデフォルトの方針決定 (短所 8 の設計部分)
- `cors_origins` / `api_keys` の安全側デフォルトをどう取るか (破壊的変更の是非)。
  例: 本番検出時 (`environment=production`) は `*` を警告 or 拒否。決定後の
  manifest 修正は Sonnet の S-9 へ委譲。web が localStorage にキーを置く前提
  (`lib/api.ts`) とのトレードオフも判断材料。

### O-8. モデル⇔プロバイダ⇔価格の単一情報源化 (F2 の構造部分)
> ✅ **完了**: `rekai/models.py` に `ModelSpec` レジストリを新設し単一情報源化。
> `router` は `PROVIDER_PREFIXES`/`provider_for_prefix` を、`pricing` は `price_table()` を、
> 各 provider の `list_models`/`list_embedding_models` は `advertised_models()` を参照。
> `test_models.py` の不変条件で「広告モデルは自 provider にルーティング + (chat は) 価格あり」
> を保証。挙動不変(/v1/models・ルーティング・コスト推定は同一)をライブ検証。
- 現状 `pricing.py` の価格表、各 provider の `list_models()`、`README.md:59` の
  ルーティング記述が三重管理で相互不整合 (o1/o3 が pricing にあるが
  `openai.py:180` の list_models に無い、等)。単一のモデルレジストリ
  (prefix→provider→price→type) に集約し、router / /v1/models / cost 推定が
  そこを引く設計。即値の更新だけなら Sonnet の S-6。

### O-9〜O-16. First Principles 棚卸しによる修正 (完了)
> ✅ 「AI ゲートウェイの必要性質 = 透過的 / 正直 / 有界 / テナント分離可能 /
> 運用可能」から逆算した棚卸しで、不足機能より先に**実害のある欠陥**が出たため
> 順序を組み替えて処理した。1 項目 = 1 コミット、すべてライブ検証済み:
> (1) 保存前レダクション、(2) idempotency のテナントスコープ化、
> (3) `REKAI_ALLOWED_PROVIDERS`、(4) `/v1/usage`・`/metrics` のテナント分離 +
> `/admin/usage`、(5) セマンティックキャッシュの正しさ 5 点、
> (6) レイテンシ 3 ヒストグラム + `rekai_requests_total` 二重計上修正、
> (7) `REKAI_MAX_CONCURRENT_REQUESTS`、(8) エラーの次元化、
> (9) `/health` の degraded、(10) ガードレール既定 `flag` + パターン精緻化、
> (11) セマンティックヒットの類似度開示。詳細は CHANGELOG の `[Unreleased]`。

### 進め方
- 1 タスク = 設計メモ (docs/ か PR 説明) → 実装 → テスト → live 検証 → CHANGELOG →
  1 コミット。O-1 / O-5 / O-6 は特に、着手前に設計を文書化してから。
