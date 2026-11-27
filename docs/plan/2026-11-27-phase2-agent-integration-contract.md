# 2026-11-27 Phase 2 AI Agent統合 実装契約（Issue #14）

- 対象 Issue: #14（Phase 2: AI Agent 統合。§6.2 Custom Tool 4 個・Household Advisor・Skills・判断根拠の構造化）
- 対象 README（正本、HEAD `972dc75` の 1657 行版）: §5 L366-463、§6 L464-632、§7.2 L646-676、§7.4 L710-728、§8.2 L747-780、§9.6 L891-908、§10 L912-981、§11.6 L1078-1086、§14.2 L1270-1317、§14.3 L1331-1337、§15 Phase 2 L1408-1425、§16.1 L1479、§16.2 L1492、§18 acceptance L1648
- 根拠としたコード: `backend/app/services/tools.py`（Phase 0 tool 層）、`backend/app/services/chat.py`（session への tool bind）、`backend/app/services/search_service.py`（Phase 1 FTS5 検索）、`backend/app/repositories/rule_repository.py`（Phase 1 正本往復）、`backend/app/agent/provider.py`・`runtime.py`（skill/session 引数）、`backend/app/config.py`、`backend/app/db/migrations/`、`skills/`
- 引用規則: 本ドキュメントの引用はすべて HEAD `972dc75` の README と、同じ転送で読み直したコードファイルから採った。行番号付き引用のみを根拠とする。

## 0. 引用の前提

### 0.1 §6.2 の Custom Tool は 4 個のみ（README 実測）

```
481	## 6.2 Custom Tools
485	以下のツールを実装する。
487	### search_rules
518	### get_rule
534	### propose_rule
566	### evaluate_rule
```

`### create_rule` は存在しない。実装対象は `search_rules`（入力 query・category・status・limit / 出力 results[]{id,title,category,status,summary}、L493-516）、`get_rule`（入力 rule_id / 出力「ルールの全文、バージョン、適用条件、例外条件」、L530-532）、`propose_rule`（入力 title・category・rule_type・applicable_to・content{basic_rule,exceptions}・reason / 出力 success・rule_id・status pending_approval、L540-563）、`evaluate_rule`（入力 rule_candidate{title,content} / 出力「関連ルール・重複の可能性・矛盾の可能性・不足している情報・確認が必要な事項」、L572-587）の 4 個である。

### 0.2 Phase 0 の tool 語彙は §6.2 と別物（コード実測）

`backend/app/services/tools.py` の tool 名集合は:

```
25	READ_TOOLS = ("read_rules", "detect_rule_conflicts")
26	WRITE_TOOLS = (
27	    "propose_rule",
28	    "update_rule_candidate",
29	    "delete_rule_candidate",
30	    "request_rule_approval",
31	)
32	RULE_TOOLS: tuple[str, ...] = READ_TOOLS + WRITE_TOOLS
```

これは Phase 0 の bridge vocabulary で、§6.2 の 4 名の内 `propose_rule` だけが同名で衝突する。先頭 docstring L1-3 は「Phase 0 では in-memory の rule store へ作用する tool 群」と明記し、`RuleToolContext`（L36-49）は `app.services.rules.RuleStore`（in-memory）を依存に持つ。Phase 1 契約 §1.2 で「Phase 2 で Phase 1 サービス backed の tool を新設し移譲する」と登記済みであり、本契約がその実施である。

### 0.3 Phase 1 の検索・正本経路が実在する（コード実測）

```
backend/app/services/search_service.py
193	def search_rules(
194	    conn: sqlite3.Connection,
195	    query: str,
196	    *,
197	    status: str = "active",
198	    category: str | None = None,
199	    limit: int = DEFAULT_SEARCH_LIMIT,
200	) -> list[dict[str, object]]:
```

README の検索制約（§8.2 L774-778「検索結果は関連度順」「検索件数は既定10・上限20とする（上限を超える指定は拒否）」「検索対象は原則としてactiveのルールとする」・L780「pending_approvalのルールは正式な判断根拠として使用しない」）は Phase 1 で clamp 込み実装済み。migration は `backend/app/db/migrations/0001_create_rule_tables.sql`・`0002_create_rule_search_index.sql` の 2 本のみで、§11.6 の `judgment_logs`（L1078-1086、列 id・conversation_id・message_id・applied_rules_json・provider・model・created_at）はまだ作られていない。`grep CREATE TABLE` で judgment・audit は 0 件。

### 0.4 Skills のロード基盤は Phase 0 で実在確認済み

```
backend/app/agent/provider.py
708	        if settings.enable_skills:
709	            kwargs["enable_skills"] = True
710	            kwargs["skill_directories"] = [_absolute_path(settings.skills_dir)]
```

`config.py` L71 `skills_dir: Path = Path("skills")`（L299 で env `SKILLS_DIR` 既定 repo `skills/`）で、README §5.2 L378・§5.3 L426 が要求する `skills/household-advisor/SKILL.md`・`skills/rule-management/SKILL.md` は既にリポジトリへ存在する。§7.4 L712-715 の pinned 実測 kwargs 一覧にも `skill_directories` が登記済み。Phase 2 の課題は「スキルを有効にする」ではなく「Household Advisor の role と一致しているかの検証・必要なら SKILL.md の §5 追従」である。

### 0.5 §6.3 の権限 matrix と PermissionBroker

```
589	## 6.3 Tool実行の権限制御
591	ルールの変更を伴う操作については、必ずアプリケーション側で権限を検証する。
593	AIのプロンプトだけで制御してはならない。
603	| 承認 | 人間の操作のみ |
605	AIによるルール候補の作成は許可するが、承認・反映はWeb API経由の人間の操作に限定する。
```

§10 L981「承認APIは認証された人間の操作のみ許可する。」・§14.3 L1337「AIから承認APIを直接実行できない。」が同様の方針を API 側で確認させる。Phase 0 の `PermissionBroker` grant 機構（tools.py L4-6 docstring「rule を変える tool は常に PermissionBroker の grant を要件にする」）はそのまま §6.2 準拠の新 tool 層へ流用する。

### 0.6 ChatService の tool bind 点（コード実測）

```
backend/app/services/chat.py
50	from app.services.tools import RuleToolContext, build_rule_tools
387	    def _rule_tool_names(self) -> Sequence[str] | None:
388	        """demo ルール tool を session へ載せるか (§7 DEMO_RULE_TOOLS)."""
389	        return None if self._settings.demo_rule_tools else ()
526	        tool_ctx = RuleToolContext(
535	            tools=build_rule_tools(tool_ctx, only=self._rule_tool_names()),
```

`config.py` L103 `demo_rule_tools: bool = True`（env `DEMO_RULE_TOOLS`、L316）。Session 生成時に 1 度だけ tool 群を bind する構造なので、新旧 tool 集合の切り替えは `_rule_tool_names()` と同じ決定点で行える。

### 0.7 ターン完了 gate は確定済み

§14.2 gate 表（L1321-1331）の 2 項目（turn 完了判定=Issue #3、認証状態=Issue #4）は closed 済みで、`docs/evidence/auth-connect-gate.md`・`make gate-auth-connect` が green。「この gate が green でない環境では、上記ケースA〜F の合否が信頼できない」（L1329-1331 要旨）の前提が満たされている。

## 1. 決定表

| # | 決定 | 理由 |
| :--- | :--- | :--- |
| 2.1 | §6.2 Custom Tool 4 個は新モジュール `backend/app/services/agent_tools.py` に実装する。名前は §6.2 正確一致の `search_rules`/`get_rule`/`propose_rule`/`evaluate_rule` で、入力・出力 schema は README L493-587 の JSON を正とする | §0.1。Phase 0 語彙の `propose_rule` と同名衝突するため同一モジュールにはしない |
| 2.2 | 新 tool の backend は Phase 1 の SQLite（`rule_repository`・`search_service`・`get_rules_connection` 相当の conn 注入）とする。in-memory `RuleStore` は使わない | §0.2・§0.3。正社は Markdown+SQLite（Phase 1 契約）。agent が参照できるルールと UI が参照するルールを同一 source にする |
| 2.3 | 設定 `AGENT_RULE_TOOLS`（env 同名、既定 on）を新設し、on のとき session には §6.2 の 4 tool を載せる。on のとき demo 集合（`DEMO_RULE_TOOLS`）は排他で載せない（`propose_rule` の名前衝突回避）。demo テストは env を明示して従来通り走らせる | §0.6 の単一 bind 点。移行期間も旧テストを壊さない |
| 2.4 | `search_rules` は `search_service.search_rules` を呼ぶ。status 既定 `active`、limit 既定 10・上限 20 の clamp を維持し、`pending_approval` を返さない。出力は §6.2 の 6 key（id・title・category・status・summary）に投影する | §0.1 L493-516・§0.3。Phase 1 の 6 key projection が §6.2 出力と一致する |
| 2.5 | `get_rule` は rule_id で rules+rule_versions の現在 version を返し、全文・version・適用条件（applicable_to・basic_rule）・例外条件（exceptions）を出す。存在しなければ成功 shape 内で `found: false` を返す（例外で turn を落とさない） | §0.1 L530-532。agent 側で tool error が turn error になる SDK 挙動を避ける（Phase 0 tool の deny pattern に倣う） |
| 2.6 | `propose_rule` は Phase 1 の作成経路（front matter 検証→pending_approval 作成→FTS 更新）を呼ぶ。PermissionBroker の grant を必須とする（grant なしは実行されず audit へ残る）。承認・active 化・削除を行う tool は一切作らない | §0.5。候補作成は許可（L605）、それ以外を tool として持たせないことで §6.3 表の「禁止」行を実装側で担保する |
| 2.7 | `evaluate_rule` は入力 rule_candidate{title,content} に対し、`search_service` で関連ルールを検出し、(a) 関連ルール一覧（id・title・version）、(b) 重複の可能性、(c) 矛盾の可能性、(d) 不足している情報、(e) 確認が必要な事項 を返す。判定は決定論的チェック（正規化一致・語彙重複度・例外条件の有無）に限る | §0.1 L572-587。本格的な矛盾検査は §16.2 Rule Auditor（L1492 以降）の将来拡張で、Phase 2 は「候補を登録せず比較できる」層まで。ケース D（矛盾提示）は agent が get_rule 2 件を読んで判断する挙動として扱う |
| 2.8 | 判断根拠の構造化は §11.6 の table を migration `0003_create_judgment_logs.sql` で追加し、§6.2 tool の実行時に適用した rule_id+version を蓄積、ターン終了時に 1 行ヘッダで保存する。provider・model は session 情報から取る | §0.3 L1078-1086。README 既存 table の実装であって新規設計ではない。§9.6 L905-908「判断時点のルールバージョンを記録」「後からルールが変更されても、過去の判断根拠を再現」に応える |
| 2.9 | Skills は既存の `skill_directories` 機構（§0.4）で有効化済みとして、session snapshot に household-advisor・rule-management の 2 skill が現れることを test で固定する。SKILL.md の本文変更は §5 との乖離がある場合のみ行う | 「実装する」が既に成立している箇所で新規コードを作らない |
| 2.10 | Household Advisor は provider の `system_message` で §6.1 の role（家族への説明・ルール候補提案・適用判断）を宣言し、SKILL.md に重複する手続を書かない | §6.1 L466-479 は判断根拠の提示を求めるが、手順の詳細は SKILL.md が正本（Phase 0 実測の分工） |
| 2.11 | テストは 2 層。(a) tool unit test: 4 tool の schema 検証・権限（grant なし deny）・Phase 1 DB に対する動作を live なしで回す。(b) §14.2 ケース A〜F の agent 挙動 test: pinned image 内の live SDK で走らせ、`make gate-agent-behavior` の新規 target にする | 完了条件「§6.2 の各 tool に test がある」+§14.2 L1270-1317。live gate は既存 prove ワークフローと同じ pinned-image 方式 |

## 2. Phase 契約

### 2.1 範囲

含む: §6.2 tool 4 個（Phase 1 backend 接続）・`AGENT_RULE_TOOLS` 切り替え・judgment_logs migration と書き込み・skills 2 件の session 確認・Household Advisor system_message・§14.2 ケース A〜F の live gate・tool unit test。

含まない（他 Phase・将来拡張へ）: Rule Auditor の定期検査（§16.2）/ MCP（§16.1）/ 判断履歴 UI・チャット UI（Phase 3）/ 旧 demo tool 群の削除（Phase 6 まで保持）/ BYOK 追加 provider（§7.1 L644 の拡張ポイントのまま）。

### 2.2 成果物

家庭内ルールを参照して回答する AI（README L1423）。具体的には §6.2 準拠 tool を載せた session で、登録済みルートを rule_id 付きで参照・提案・比較でき、判断根拠が judgment_logs に再現可能な状態。

### 2.3 スライス分割（コミット単位）

| スライス | 内容 | 検証 |
| :--- | :--- | :--- |
| A | 本契約文書（コミット 1）| docs のみ |
| B | `agent_tools.py` の §6.2 4 tool + tool unit test + `AGENT_RULE_TOOLS` 設定と ChatService bind | tool unit test + `make gate` 再走 |
| C | skills 2 件の session 確認 test + Household Advisor system_message | provider unit test（snapshot の skills 2 件）|
| D | migration 0003 judgment_logs + tool 実行〜ターン終了の保存経路 + unit test | migration test + `make gate` |
| E | ケース A〜F live test + `make gate-agent-behavior` target + 証跡（`docs/evidence/agent-behavior.md`）| 各ケース green + Phase 0/1 gate 再走 |

各スライスは独立に green にして個別 PR にする（Phase 1 の慣習）。E で §15 Phase 2 完了条件を判定し、Issue #14 を close する。

### 2.4 完了条件（README §15 L1425 + §14.2）

1. 登録済みルールを参照し、ルール ID とともに回答できる（ケース A・C の応答に rule id が含まれることを live test で確認）。
2. ケース B で未登録の明示、D で両ルール提示、E で pending_approval 候補作成＋承認待ち、F で登録しない、が live test で確認できる。
3. `search_rules`/`get_rule`/`propose_rule`/`evaluate_rule` すべてに unit test がある。
4. judgment_logs に適用ルール ID・version・provider・model が残る（§9.6・§11.6）。
5. Phase 0/1 の全 gate 再走が green（`make gate` 含む）。
