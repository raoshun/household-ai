# 2026-11-18 Phase 1 ルール管理基盤 実装契約（Issue #8）

- 対象 Issue: #8（Phase 1: Markdown ルール正本・YAML Front Matter・Pydantic 検証・SQLite インデックス・FTS5 検索・バージョン管理）
- 対象 README（正本、HEAD `e681aba` の 1519 行版）: §2.1 L52-68、§4 L228-363、§6.2 L479-586、§6.3 L587-604、§7 L632-698、§8 L699-759、§9.3-§9.6 L808-876、§10 L877-942、§11 L943-1022、§12 L1023-1066、§13.2/§13.4 L1089-1151、§14 L1152-1230、§15 L1231-1341、§16 L1342-1403、§17 L1404-1424、§18 L1425-1461、§20 L1500-1519
- 根拠としたコード: `backend/app/services/rules.py`（ハブの in-memory rule store）、`backend/app/services/tools.py`（tool 権限 matrix）、`backend/app/config.py`、`docker-compose.yml`、`Makefile`、`docs/plan/2026-11-11-phase0-ui-implementation-contract.md` §4 HTTP 面
- 引用規則: 本ドキュメントの引用はすべて HEAD `e681aba` の README と、同じ転送で読み直したコードファイルから採った。行番号付き引用のみを根拠とする。

## 0. 引用の前提

### 0.1 README は Phase 1 の SQLite table を「作る」としか書いていない（§11 L945）

```
943	# 11. データベース設計    (L943)
945	SQLiteに以下のテーブルを作成する。    (L945)

966	## 11.3 rules    (L966)
968	- id    (L968)
969	- title    (L969)
970	- category    (L970)
971	- status    (L971)
972	- rule_type    (L972)
973	- priority    (L973)
974	- current_version    (L974)
975	- file_path    (L975)
976	- created_at    (L976)
977	- updated_at    (L977)
```

FTS5 の index table は README のどこにも命名されていない（§11 L943-1022、§8.1 L703 に「初期段階ではSQLite FTS5を利用する。」とあるだけ）。Phase 1 が名前を決める。

### 0.2 Phase 0 backend には SQLite が一切ない（実測）

repo 追跡 code で `sqlite3` / `CREATE TABLE` / `INSERT INTO` / `CREATE VIRTUAL TABLE` は `*.py`・`*.sql` のいずれでも 0 件、`bridge_claim_receipts` は README・docs・backend・tests のどこにも無く、`backend/app` に `main.py`・`api/`・`models/`・`repositories/`・`db/` は存在しない、`data/` は gitignore 対象で worktree 上に存在しない。`backend/app/config.py` L158-165 の `rules_dir`（`data/rules`）と `database_path`（`data/household.db`）は settings の宣言のみで、storage も SQLite も Phase 1 が初めて作成する（`.gitignore` L8-9 は「実行時に生成されるデータ (ルール Markdown・SQLite・Copilot SDK のランタイム状態)」として `data/` を除外）。

### 0.3 Phase 0 の rule store の現在形（`backend/app/services/rules.py` から）

```
12	RuleKind = str  # Phase 0 は "intent" のみ (§15)。                    (L12)
13	RuleStatus = str  # "pending" | "active" | "tombstoned"            (L13)
14	RuleSource = str  # "agent" | "family_ui" | "migration"            (L14)
16	RULE_STATUSES = ("pending", "active", "tombstoned")                (L16)
17	RULE_SOURCES = ("agent", "family_ui", "migration")                  (L17)
18	CONFLICT_KINDS = ("intent_duplicate", "time_overlap")              (L18)

44	@dataclass(slots=True)                                              (L44)
45	class Rule:                                                        (L45)
48	    rule_id: str                                                   (L48)
49	    title: str                                                     (L49)
50	    content: str                                                   (L50)
51	    status: RuleStatus = "pending"                                 (L51)
52	    kind: RuleKind = "intent"                                      (L52)
53	    source: RuleSource = "agent"                                   (L53)
54	    created_by: str = "family"                                     (L54)
58	    approved_at: str | None = None                                 (L58)
60	    revision: int = 1                                              (L60)
61	    notes: list[dict[str, Any]] = field(default_factory=list)     (L61)
```

`Rule` に `category` も `rule_type` も `priority` も `tags` も `applicable_to` も `statement` も無い（`data/rules/**` の `category` prefix vocabulary も hub code のどこにも無い: `housework|safety|childcare|pets|CATEGORY` は `tests/proofs/phase0_live_proof.py` の tool 名 `household_list_housework` にしか現れない）。`RuleConflict`（L90 以降）と `CONFLICT_KINDS`（L18）は hub 内部の語彙であり、README には conflict kind の enum 定義が無い（§16.2 L1356「ルールの矛盾や重複を定期的に検査する。」と検査項目 L1360-1364（「内容が重複するルール」L1360、「相互に矛盾するルール」L1361、「適用条件が曖昧なルール」L1362、「例外条件が不足しているルール」L1363、「長期間更新されていないルール」L1364）を列挙しているだけで、kind の enum も `evaluate_rule` との対応も書かない。hub の conflict kind は rules.py L18 の `CONFLICT_KINDS = ("intent_duplicate", "time_overlap")` の 2 値のみ。README に "intent_duplicate"・"time_overlap"・公平性 は 0 件で、`安全性` が出現するのは §12 見出し L1023「# 12. セキュリティ・安全性」のみ。Phase 1 の範囲は §15 L1258「## Phase 1：ルール管理基盤」の 実装内容 L1262-1267、成果物 L1270「ルール管理API。」、完了条件 L1273「ルールを登録・検索・更新できる。」までであり、矛盾・重複の検査は §16 将来拡張（L1344「初期実装では不要だが、以下の拡張を妨げない設計にする。」、§16.2 Rule Auditor L1354）側にある）。

hub の tool 面（`backend/app/services/tools.py`）:

```
25	READ_TOOLS = ("read_rules", "detect_rule_conflicts")               (L25)
26	WRITE_TOOLS = (                                                  (L26)
27	    "propose_rule",                                            (L27)
28	    "update_rule_candidate",                                   (L28)
29	    "delete_rule_candidate",                                   (L29)
30	    "request_rule_approval",                                   (L30)
31	)
```

read_rules の入力は `{status: "active"|<state>|all|None, query, limit}`（limit 既定 20、`max(1, min(limit, 50))` で clamp、`_tool_read_rules` L151-178）で、返す各行は `_rule_brief`（L78-87: `content`（120 字 preview）・`kind`・`revision`・`rule_id`・`source`・`status`・`title`）。これは README §6.2 の tool 契約ではなく Phase 2 の対象（§15 Phase 2 heading L1275、実装内容 lists L1279-1286、tool 名 L1282-1285）。

### 0.4 ハブ docstring の § 参照は README の番号と一致しない

`services/rules.py` L1 は「§9.1 family rule (§10.1 `/api/rules`) と §9.4 pending → active 判定を保持する。」、`services/tools.py` L1 は「§6.2 Custom Tool 定義と §6.3 tool 権限 matrix enforcement を保持する。」（§6.3 は L3 でも繰り返す）、`services/rules.py` L380 は「§9.4 pending → pending (tombstone しない) + §11 record。」、L434 は「§9.4 提案 draft 対 existing active (§11/§9.4 conflict heuristic)。」と書く （`grep -no '§[0-9.]+' rules.py` の実測: L1/L12/L46/L92/L117/L131/L168/L170/L245/L249/L343/L369/L380/L393/L421/L434 のみ）。README では §9.1 は「画面構成」L762、§10.1 は「Chat API」L881、§11.3 は `rules` テーブル L966、§11 は L943 であり、hub の `§` 参照は README の § 番号と対応しない。Phase 0 契約の § 体系を Phase 1 の正本参照へ流用しない。

## 1. 決定表（固定済み）

| # | 論点 | 決定 | 根拠 |
| :--- | :--- | :--- | :--- |
| 1.1 | README 優先 | README §4・§8・§10・§11・§12・§13.4・§14・§15・§17・§18・§20 を正本とし、Phase 1 の実装単位のみを追加する。実装コミットは `#8`、README 更新は `#2`。 | ユーザー決定、§13.4 L1146-1148 |
| 1.2 | ルール正本 | Markdown ファイル。DB は検索用インデックス・承認状態・履歴・メタデータのみ保持。 | §4.1 L234/L236/L240 |
| 1.3 | ルール identity | 1 rule = 1 系の `rules` row + effective バージョン pointer + pending 候補 version。提案の新規作成、改訂、旧版復元、active 化、archived、拒否は同一 `rules.id` の状態遷移とし、識別子を切り替えない。status 遷移だけで version は上げない。 | §4.6 L337-360、§9.5 L845-854、§14.3 L1212-1216、§11.3-§11.5 |
| 1.4 | カテゴリ | `rules.category`（§11.3 L970）と front matter `category`（§4.2 L248）が一意の source。`data/rules/` 直下の 6 slug（parenting・education・health・lifestyle・finance・general、§3 L199-204）を Phase 1 が定義する。カテゴリ別 table も hub 由来 vocabulary も作らない。 | §3 L199-204、§4.2 L248、§9.3 L812 |
| 1.5 | 検索対象 | ルール名・基本方針・適用条件・例外条件・判断基準・タグの 6 項目（§8.1 L707-712）。カテゴリと status は検索語ではなく §8.2 のフィルタ stage（Filter by Status / Category L732）。Markdown 本文全文は検索対象にせず、§4.2 の 4 見出し（基本方針 L270 / 適用条件 L274 / 例外条件 L278 / 判断基準 L282）と front matter から §8.1 の 6 項目を抽出して index に投影する。README の rules 一覧 row（L915）には query が書かれていないため、カテゴリ・status の絞り込み表現は Phase 1 の query param 設計とする（§2.10.1 Q-01）。 | §8.1 L705-712、§8.2 L726-745、§10.2 L915-921 |
| 1.6 | 検索対象 status | 既定は active のみ（「検索対象は原則としてactiveのルールとする。」）。pending_approval は正式な判断根拠にしない。 | §8.2 L743/L745、§14.1 L1158-1159 |
| 1.7 | 検索 limit | Phase 1 backend の検索 API は `limit` を既定 10・上限 20 で clamping する。README に検索 limit を課す sentence は無い（README grep: 「上限」「表示件数」「件数」はいずれも 0 件）。根拠は §6.2 入力例 `"limit": 10` L496 と §14.1 L1157「無関係なルールが過剰に返されない。」の 2 つで、上限値 20 は Phase 1 判断として §2.10.1 Q-03 に登記する。 | §6.2 L492-497、§8.2 L741、§14.1 L1157 |
| 1.8 | 検索結果形 | 既定 projection は §6.2 `search_rules` 出力の 5 key（id L506・title L507・category L508・status L509・summary L510）に README の `current_version`（§11.3 L974）を加えた 6 key とする。§9.6 L871「判断時点のルールバージョンを記録すること」に従い、full Markdown body や全文 section mirror を既定の検索結果に入れない。 | §6.2 L503-513、§9.6 L863/L871、§11.3 L974 |
| 1.9 | FTS5 CJK 分割 | canonical CJK text を 2-3 char の contiguous n-gram へ変換し、ASCII runs（length ≥ 3）を `ascii_terms` へ入れる。検索 query も同じ変換を通し、term group 間は AND、group 内 alternatives は OR。README の検索フローは tokenizer を指定しない（§8.2 L723-726）ため Phase 1 design として §2.6 で決める。実測は §4。 | §8.1 L703、§8.2 L726、§17 L1417、§4（probe） |
| 1.10 | バージョン | 版数は monotonic で reuse しない。内容 hash が同じ candidates は reject する。旧 version を restore るときは旧ファイルを書き換えず、新しい version number で copy する（revision と restoration の両方で candidate version を作る）。status 変化は version を上げない。README §11.3 rules 行に version 列は無く `current_version` pointer（L974）だけ、version 一覧は §11.4（L981-988）。 | §4.6 L337（上書き禁止）/L344・L345・L346（version files）/L349（最新版参照）/L360（復元）、§11.3 L974、§11.4 L981-988、§14.3 L1215 |
| 1.11 | 承認 | 提案・改訂・復元は `approvals`（§11.5）と人間のHTTP 操作経由だけ。AI tool に承認・反映を許可しない。 | §4.5 L331、§6.3 L593-603、§10.3 L927-932、§12.4 L1053-1057、§18 L1437 |
| 1.12 | HTTP scope | Phase 1 の成果物は §15 L1270「ルール管理API。」。rules API（§10.2 L915-921 の 7 row）と approvals API（§10.3 L927-930 の 4 row）、それらが使う repository / service / codec / index のみはこの契約で決める。hub の `ui-bridge` が既に確定させている rules / approvals / history 用語（Phase 0 UI 契約 §4）と衝突させたままにはしない。chat / session / AG-UI bridge は §12 のスコープ外として Phase 2 の HTTP 契約で決める。 | §15 L1266-1273、§10.2 L913-921、§10.3 L925-932、Phase 0 UI 契約 L71-112 |
| 1.13 | 検証 | unit + integration pytest を pinned image の make targets で走る。host へ pip install しない。 | §18 L1438/L1439、§20 L1504-1518 |
| 1.14 | Phase 0 との境界 | hub の `services/rules.py` / `services/tools.py` は残置し、hub vocabulary と README vocabulary を adapter で橋渡しする。hub 内部 API・frames・grant 契約を変えない。 | README §13.4 実装済み構成 L1110-1151（api L1122・ui-bridge L1123）、Phase 0 契約 §2 frames L35-57・§5 grant L113-122、hub code `backend/app/services/rules.py` L16-18/L248・`tools.py` L25（HEAD で追跡） |
| 1.15 | gate 維持 | 既存の gate（`make gate` L52-53）と proof gate（L55-56）、pinned SDK probe（`probe` target L58-59）は変更しない。Phase 1 は rules 用の unit / integration pytest を追加し、`make gate` の既存 checks を狭めない。 | `Makefile` L52-59、§20 L1500-1519 |
| 1.16 | code 配置 | Phase 1 が新設する backend file は README §3 tree の名前に従う（`main.py` L136、`api/rules.py` L141・`api/approvals.py` L142、`services/rule_service.py` L147・`services/search_service.py` L148、`models/rule.py` L161、`repositories/rule_repository.py` L166、`db/database.py` L171・`db/migrations/` L172）。test は `tests/unit/` L217 と `tests/integration/` L218 へ作る。 | README §3 tree L131-172・L216-219。hub 既存 file は §1 決定 1.14 で残置（差異 D-11、§2.4.4）。 |
| 1.17 | Markdown codec の literal layout | front matter は README §4.2 サンプル L245-268 の literal （delimiter L245/L268、group 空行、配列 item 2 space indent、unquoted ISO `T` timestamp）を application 側 manual writer で書き、scalar と list item の quoting だけ file 単位の `yaml.dump` を使わず 1 値ずつ render する。parser は `---` delimiter を hand-slice で切り、YAML 本文だけ `yaml.safe_load` へ渡す。PyYAML の document 出力と Pydantic の dump を正本文の layout generator にしない。body は `#` 見出し 4 節の literal を保持する | README §4.2 L244-285、実測 §2.4.5、差異 D-12 |
| 1.18 | 未承認 rule の `approved_by` の front matter 表記 | 13 attribute を常に書く方針を保ち、未承認 は `approved_by: null` の explicit null で書く（attribute を省略した form は writer が出さない）。parse は省略 form を relaxed input として受理し model の default `None` へ復元する。README に未承認 の表記例は無い | README §4.3 L303「承認者」のみ、§4.2 sample L267 `approved_by: user`、実測 §2.4.5、Q-01 |
## 2. Phase 1 契約

### 2.1 SQLite スキーマ

README §11 L945「SQLiteに以下のテーブルを作成する。」の下に列挙された `rules`（§11.3 L968-977: id / title / category / status / rule_type / priority / current_version / file_path / created_at / updated_at）と `rule_versions`（§11.4 L981-988）の列名を原則そのまま採用する。README に無い列を追加する場合は §2.9 差異記録台帳に記載する。本文の正本は Markdown、SQLite は検索 index・承認状態・履歴・メタデータ（§4.1 L236-240）である。

```
rules         (id, title, category, status, rule_type, priority, current_version,
               file_path, created_at, updated_at)
rule_versions (id, rule_id, version, content_hash, change_reason, created_by,
               approved_by, created_at)
```

`rules` の Phase 1 制約。`id` は README §4.2 L246 と §6.2 L559 の例にならい `<category>-<3 digit>`（例 `parenting-001`）。`category` は §3 L199-204 の 6 slug（parenting・education・health・lifestyle・finance・general）のいずれか。`status` は §4.5 L325-329 の 5 値（draft L325、pending_approval L326、active L327、rejected L328、archived L329）、`rule_type` は §4.4 L311-315 の 5 値（strict・principle・preference・guideline・exception）。`priority` は §11.3 L973 と §4.3 L297 に存在し、§4.2 の例 L254 は `priority: normal` だが、README に値域の列挙は無い。Phase 1 では `TEXT NOT NULL DEFAULT 'normal'` とし、許容値は §2.9 差異 D-03 として #2 の README 改訂で確定する。`file_path` は `data/rules/` からの相対（§4.6 L342-346）。`created_at` / `updated_at` は ISO 8601 +09:00（§17 L1415「Asia/Tokyo」）。

### 2.2 状態遷移（README §4.5 L325-333 / §9.5 L845-854 / §10.3 L932）

| 遷移 | 誰が | README の根拠 | Phase 1 実装 |
| :--- | :--- | :--- | :--- |
| （新規）→ pending_approval | AI 提案は必ずここから (L331) | §4.5 L331、§6.2 propose_rule 出力 `"status": "pending_approval"` L560 | 候補 Markdown を書き、`rules.status='pending_approval'` + 未 active の version 行を作る |
| pending_approval → active | 人間の承認操作のみ (L333) | §4.5 L333、§10.3 人間のみ L932、操作「承認」L849、§12.4 L1053-1055 | 承認 API/操作が有效版ポインタを進め、`rule_versions.approved_by` (L987) と `approvals` の `reviewed_by` (L996) / `reviewed_at` (L999) を埋める（README に `approved_at` 列は無い） |
| active の内容変更 → 新しい version | 人間のみ（AI は §12.4 L1053-1055 で直接変更も直接承認もできない） | §4.6 上書きしない L337、 version files L342-344、§10.2 versions 一覧 L919 | 新版 Markdown を追加し `rules.current_version`（L974）を差し替え、置換前の active 版は `rule_versions` 履歴へ残す |
| 過去 version → active（復元） | 人間のみ（§12.4 L1055「AIが作成したルールは必ず承認待ちにする。」と整合） | §4.6 参照・復元 L360、README API POST restore L920 | 旧 version の本文をコピーして新 version として登録（番号は使い回さない） |
| pending_approval → 却下 | 人間の却下操作 L851 | §9.5 却下 L851、README status `rejected` L328（操作後の状態の明記なし） | 候補を active に昇格させない。本文の扱い（残す/削除）は open question Q-10 |
| active → draft / archived | README に遷移条件の記載なし | §4.5 L325-329 に列挙のみ（L328 rejected、L329 archived） | Phase 1 では遷移を公開しない（enum に含め、SQL 制約だけ保持）。差異 D-06、#2 で README 追記を要求 |

### 2.3 version と判断ログ

1. `version` はルールごとに 1 以上の整数とし、新しいほど大きい値を使う（番号の使い回しなし）。README §4.6 の tree 例（block L341-347: `data/rules/parenting/` L342・`parenting-001/` L343・`v1.md` L344・`v2.md` L345・`v3.md` L346）と API の `version` 参照 L918-919 に従う。
2. 最新版 (`latest version`) は README の `rules.current_version`（§11.3 L974）が指す。README §4.6 L349 最新版参照。
3. 判断ログには判断時点の version を保存する（README §14.3 L1214、§9.6 L871「判断時点のルールバージョンを記録すること」と §11.6 `applied_rules_json` L1006）。
4. README が指定する DB table 名は `judgment_logs`（§11.6 L1001）。Phase 0 の HTTP 契約は `GET /api/history/judgments`（docs/plan/2026-11-11-phase0-ui-implementation-contract.md L100）と `POST /api/judgments`（同 L101）である。table 名と path の対応は §2.9 差異記録台帳 D-01 に登記し、Phase 1 では table 名を README と変えない。

### 2.4 Markdown / YAML コーデック

#### 2.4.1 front matter（README §4.3 L291-303 の 13 属性）

`id` `title` `category` `status` `version` `rule_type` `priority` `applicable_to` `tags` `created_at` `updated_at` `created_by` `approved_by`

Phase 1 で追加する属性（README の属性一覧に無いが、他の節が必要とする）:
- `summary`（README §6.2 search_rules 出力例 L510）は §4.3 属性一覧（L291-303）にも rules 列（L968-977）にも無い key。Phase 1 は DB の `rule_versions.summary` text 列（§11.4 L981-988 に無い列を §1 決定 1.1 の README-first で追加）へ §4.2 本文 4 見出し（L270/L274/L278/L282）直後の 1 行目を寫し、検索 projection へ写す。生成方法の規定が無いことは差異 D-04・open question Q-06 として登記する。
- `approved_at` は front matter に追加しない。README §4.2 サンプル（L245-268）・§4.3 一覧（L291-303）・§11.4（L981-988）・§11.5（L992-999）のいずれにも `approved_at` は無い（grep 0 件）。承認日時は README の列 `rule_versions.approved_by`（L987）と `approvals.reviewed_at`（L999）で保持し、README に無い時刻列を Phase 1 で追加する必要があるかは open question Q-01 に登記する。

#### 2.4.2 本文（README §4.2 heading L242、ファイル例 L244-285 の中の本文見出し L270-284）

正本 Markdown の本文は `#` 見出し 4 節をこの順序で持つ。

1. `# 基本方針`
2. `# 適用条件`
3. `# 例外条件`
4. `# 判断基準`

- 4 見出しは必須（Pydantic で検証）。見出しごとの本文は自然言語（README §4.1 L232「家庭内ルールはMarkdownファイルとして保存する。」、L234「YAML Front Matterで構造化情報を保持し、本文は自然言語で記述する。」）。追加見出しは README §4.2 の例外なので Phase 1 では許可しない。
- README §9.4（詳細画面の表示項目 L834-841）は「ルール全文 L834」「適用対象 L835」「適用条件 L836」「例外条件 L837」「判断基準 L838」「現在のバージョン L839」「更新履歴 L840」「関連ルール L841」を要求する一方、§4.2 の例の本文見出しは `# 基本方針` L270・`# 適用条件` L274・`# 例外条件` L278・`# 判断基準` L282（level 1）で、UI 表示項目 8 個と本文見出し 4 節は 1 対 1 ではない。§4.2 の正本見出し 4 節を先行し、UI 画面側は §11.3 rules 行の front matter 属性（§9.4 L835「適用対象」）と version 情報（L839/L840）を API projection として補う（差異記録 D-02 / open question Q-06）。

#### 2.4.3 ファイル配置

- 1 rule 1 version = 1 Markdown: `data/rules/<category>/<rule-id>/v<n>.md`。README の例は tree block L341-347 に `data/rules/parenting/`（L342）／`parenting-001/`（L343）／`v1.md`（L344）・`v2.md`（L345）・`v3.md`（L346）と書かれている。ファイル名の詳細規約は README に明文が無い（差異 D-09）。
- category 直下は README §3 L198-204 の 6 ディレクトリ（parenting: L199、education: L200、health: L201、lifestyle: L202、finance: L203、general: L204）のみ。追加 category を作らない（§1 決定 1.4）。
- README に `index.md` 等の目次ファイルは無い（grep 0 件）。Markdown 側へ追加ファイルを作らない。
- SQLite（README `data/household.db` L197）は git 管理外の生成物。`.gitignore` L8-9 で既に無視される `data/` 以下に置く。

#### 2.4.4 backend code 配置（README §3 L131-172・L216-219 と hub HEAD）

- Phase 1 が new spec の file 名で新設する先は README §3 tree の実測行に従う: `backend/app/main.py`（L136）、`api/rules.py`（L141）・`api/approvals.py`（L142）・`api/history.py`（L143）、`services/rule_service.py`（L147）・`services/search_service.py`（L148）・`services/approval_service.py`（L149）、`models/rule.py`（L161）、`repositories/rule_repository.py`（L166）。README の `db/` は `database.py`（L171）と `migrations/`（L172）のみを挙げるので、Phase 1 の schema 定義は `db/migrations/` 配下の .sql file に置き、README に無い `db/schema.py` は作らない。README tree の file 群は hub に存在しない（次 bullet の実測 17 file）。
- これらの file は HEAD に存在しない（`git ls-files backend` で 0 件）。hub 既存の `services/rules.py`・`services/tools.py`・`services/chat.py` は §1 決定 1.14 で変更せず、rules 側の repository / service が hub の `RuleStore` を呼ぶ adapter で橋渡しする（差異 D-11）。
- test は README §3 の tree 通り `tests/unit/`（L217）と `tests/integration/`（L218）へ作成する。HEAD に有る `tests/proofs/` は README の tree に無い配置で（`make proof` gate Makefile L55-56 で実行）、Phase 1 の test 配置へ混ぜない。`tests/e2e/`（L219）は Phase 1 では作らない。
- 実行は pinned image の gate 経由（`make lint` L65-66、`make test` L68-69）。`backend/pyproject.toml` の pytest 設定は container 内 cwd（`backend/`）から `../tests` を指す（`testpaths = ["../tests"]` L49）。host へ pip install しない。
- hub 資産の実測 inventory（2026-11-19、`git ls-tree -r --name-only origin/main -- backend` の 17 file: `app/__init__.py`・`app/clock.py`・`app/config.py`・`agent/` 5 file・`services/` 9 file）。`api/`・`models/`・`repositories/`・`db/` は 0 file（差異 D-11）で、Phase 1 が新設する。

#### 2.4.5 コーデック実測（pinned image の Python 3.12.12 / PyYAML 6.0.3）

2026-11-19 に pinned image `household-ai-runtime-test:0.1.0`（Makefile L5）内の python3 で実測した（Python 3.12.12、PyYAML 6.0.3、Pydantic 2.14.3、ruff 0.17.4、`sqlite3.sqlite_version` 3.40.1。pinned 版は `backend/pyproject.toml` L5・L12・L15・L24）。README §4.2 サンプル（code fence L244-285）の layout は:

- `---` delimiter L245（open）と L268（close）の間に 13 key、key 群は空行 L249・L252・L255・L258・L262・L265 で 7 group（id/title/category・status/version・rule_type/priority・applicable_to・tags・created_at/updated_at・created_by/approved_by）へ分かれる。
- 配列 item は parent key より 2 space 深い indent（`applicable_to:` L256→`  - children` L257、`tags:` L259→`  - food` L260・`  - parenting` L261）。
- timestamp は `2026-11-07T10:00:00+09:00` の ISO 表記（秒精度 + 固定 offset、L263・L264）。
- 本文は code fence 内で `#` 見出し 4 節 L270-284、fence 閉じ L285。

PyYAML 6.0.3 と Pydantic 2.14.3 の実測:

1. `yaml.dump(value, sort_keys=False, allow_unicode=True)` は block sequence item を column 0 へ出す（`- children` の dash indent 0）。`indent=2` を足しても dash の indent は 0 のまま（README の 2 space indent を再現しない）。
2. `datetime` は `2026-11-07 10:00:00+09:00`（space 区切り）で dump され、README の `T` 表記にならない。
3. closing `---` を含む front matter text を `yaml.load` すると `ComposerError: expected a single document in the stream`（README の file layout は closing delimiter を YAML stream 内に持つ L268）。
4. block sequence 空 list は `key: []`（flow style）として render される。
5. front matter text の key 順は `yaml.safe_load` で保存される（README サンプル L246-267 を load すると 13 key が §4.3 一覧 L291-303 と同順で返り、`version` は `int`、`applicable_to`・`tags` は `list`）。
6. unquoted の `2026-11-07T10:00:00+09:00` は `yaml.safe_load` で `datetime`（`timezone(timedelta(seconds=32400))`）に復元され、quoted すると `str` になる。Pydantic の `model_dump_json()` / `model_dump(mode="json")` は datetime を quoted ISO string にする（CJK は escape されず `お菓子の与え方` のまま）。
7. `default_flow_style=True` の scalar dump は長い日本語 string を折り返さない: 160 char の string（`対応方針` x 40）で `width` 未指定・`width=1_000_000`・`width=20` の 3 種の output が byte 一致し、scalar line 1 つと document end `...` line 1 つの 2 line だけ返る（`default_flow_style=False` でも折り返さない）。load すると同一の 160 char `str` へ復元される。front matter の 1 line 保証に `width` 指定は不要（実測 1 の block sequence dash indent 問題とは別件）。
8. 型と解釈が衝突する string scalar（`true` `yes` `no` `null` `~` `001` `2026` `2026-01-02` `-1` `1.5` `a: b` `b #c` `*` 空 string）は PyYAML 6.0.3 が single quote で囲み、`yaml.safe_load` で入力と同一の Python `str` へ復元する。quoted にならない例は `parenting-001` `お菓子の与え方` `「与える」`。document end `...` line を落としても value は欠損しない。
9. Pydantic の `model_dump(mode="python")` が返す `StrEnum` member を `yaml.dump` へ渡すと `!!python/object/apply:app.models.rule.RuleCategory [parenting]` の python tag になり、`yaml.safe_load` は `yaml.constructor.ConstructorError` で reject する（1 要素 list の item でも同じ: `- !!python/object/apply:app.models.rule.RuleCategory` と `- parenting` の 2 line）。`yaml.unsafe_load` は復元できるが正本 parser は safe loader を使うため、writer は `.value` の plain string を渡す。

契約化する handling（§1 決定 1.17）:

- Writer: 13 attribute を README §4.3 一覧の順（L291-303）で書き、group 空行（L249・L252・L255・L258・L262・L265 相当）、配列 item 2 space indent、timestamp は `datetime.isoformat()` の unquoted ISO（README L263-264 相当）を manual writer で出す。quoting だけ 1 値ずつの `yaml.dump` へ委譲する: scalar は `yaml.dump(value, default_flow_style=True, allow_unicode=True, sort_keys=False)`、list item は `yaml.dump(item, default_flow_style=True, allow_unicode=True, sort_keys=False)` の scalar 表記へ委譲し prefix `  - ` だけ手で付ける（`yaml.dump([item], default_flow_style=False, ...)` でも dash は column 0 の 1 line だけ返るが、dash を除去した scalar 表記は flow style scalar と同じ quoting になるため scalar 用の 委譲を reused する。実測 7 より 1 line 化は `width` に依存しない）。enum member は 実測 9 の python tag を避けるため `.value` を渡す。実測で plain scalar の output 末尾には document end を示す `...` の line が付く（`'plain'` → `'plain\n...\n'`）のでその line を落とし、quoted scalar や 1 要素 list には付かない（`'yes'` → `"'yes'\n"`、`['food']` → `"- 'food'\n"`）。render 結果に newline が残る value（`'a\nb'` → `"'a\n\n  b'\n"`）は front matter の scalar として reject する。
- Parser: file 先頭の `---` と closing `---` を hand-slice で切り、front matter text（L246-267 相当）を `yaml.safe_load` へ、本文（L270 以降）を body parser へ separate に渡す。
- `null` の表現（§1 決定 1.18）: writer は 未承認 の `approved_by` を `approved_by: null` の scalar で書く。省略形の `approved_by:` は relaxed な input として受理し model default の `None` で復元する（README サンプル L267 は 承認済み の sample なので 省略形の 例ではない）。
- 往復の proof（実測済み）: README サンプル front matter block L246-267（22 line）を `yaml.safe_load` → 上記 manual writer で render すると original と byte 一致した（2026-11-19 実測、key 順 README §4.3 L291-303、blank 挿入位置は index 2・4・6・7・8・10 の 後、配列 item は parent の 2 space 下、timestamp は `datetime.isoformat()` の unquoted ISO）。この往復 byte 一致は Phase 1 の test で固定する。
- Test 配置: render → parse → render の往復が byte 一致することと README サンプル（L246-267 の front matter text と L270-284 の body）の parse は `tests/unit/test_rule_markdown_codec.py`（README §3 L217 の directory）で検証し、`data/rules/<category>/<rule-id>/v<n>.md` への write と read back（§2.4.3・§1 決定 1.10）は `tests/integration/test_rule_markdown_files.py`（README §3 L218 の directory）で検証する。Pydantic model の field 順が README §4.3 一覧 L291-303 の順であることも unit test で assert する。golden は README §4.2 の サンプル file 本体 L245-284 の 40 line であり、unit test 内 の 定数 `README_SAMPLE_RULE` へ byte 一致で inline 保持する: gate の test / lint container は `backend/app` と `tests` だけを bind mount し（Makefile L63 の `SRC_MOUNTS`）、pinned image の `/app` には README.md が存在しないため、test から README を read できない。

### 2.5 検索契約

#### 2.5.1 検索対象（README §8.1 検索方式 L701、検索対象 L705-712）

ルール名・基本方針・適用条件・例外条件・判断基準・タグ

#### 2.5.2 フィルタと件数

- status: 「検索対象は原則として active のルールとする」（README §8.2 L743）。pending_approval は判断根拠に使用しない（L745）。デフォルトは `status=active`。
- category: 1 値で絞る（README §8.2 Filter ノード L732、§9.3「カテゴリ別表示」L812・表示項目「カテゴリ」L825、§11.3 `category` 列 L970）。
- limit: README に上限の規定は無い。既定 10 は §6.2 search_rules 例の `"limit": 10`（L496）から取り、上限 20 は §1 決定 1.7 と §14.1 L1157「無関係なルールが過剰に返されない。」に基づく Phase 1 判断として §2.10.1 Q-03 に登記する。

#### 2.5.3 並びと出力

- 並びは関連度順（README §8.2 L741「検索結果は関連度順で返す」）。
- 検索結果 1 件の key は id・title・category・status（README §6.2 出力例 block L502-514 の L506-509）に `summary`（L510）を加えた 5 件。`summary` は §4.3 属性一覧 L291-303・rules 列 L968-977 に無い key（差異 D-04・Q-06）。
- ルール取得の本文出力は §9.4「ルール全文」L834 に従う。README §6.2 `get_rule`（heading L516、説明 L518、入力 block L522-526 の key `rule_id` L524）はコードブロックの出力例を持たず、出力を prose で「ルールの全文、バージョン、適用条件、例外条件を返す。」（L530）とだけ規定する。path `/api/rules/{id}`（L917）と version 併記 key の規定が無い（差異 D-05 / open question Q-04）。

#### 2.5.4 FTS5 と CJK 分割（README §17 L1417）

README は SQLite FTS5 による全文検索の技術選定と CJK tokenizer の仕様を持たない（§8.2 のフローに tokenizer 名は現れず、§17 の table L1406-1421 の L1417 行（項目「検索」、要件「FTS5」）が技術選定を表すのみ）。Phase 1 では以下を設計判断として契約化する。

1.検索用インデックスは正本 Markdown から再構築できる（README §4.1 L238、再インデックス L240）。
2. 検索 flow は README §8.2 の text-art のノードを模する: Query Analysis L720 → Keyword Extraction L723 → SQLite FTS5 Search L726 → Candidate Rules L729 → Filter by Status / Category L732 → Return Relevant Rules L735 → Agent Evaluation L738（L716-739）。README に BM25・tokenizer・limit・offset の語彙は無い。Phase 1 に flow の工程追加はしない（§1 決定 1.14）。
3. 検索結果の本文表示は FTS5 行ではなく正本 Markdown file から読む（README §4.1 L234、§8.2 flow の return ノード L735）。README §8.2 の flow に本文抽出という工程名は無い。
4. FTS 行は正本ではない（README §4.1 L236「ルールの正本はMarkdownファイルとする。」、L238「SQLiteには検索用インデックス、承認状態、履歴、メタデータを保持する。」）。検索結果の本文が必要な場合は Markdown を読む。

### 2.6 CJK 全文検索のトークン化

Phase 0 には全文検索が無い（Phase 0 契約 docs/plan/2026-11-11：`/api/rules/search` 無し）。Phase 1 で新規実装するため、実装前に pinned image で probe を書く。

- 検証する挙動: 日本語文を FTS5 unicode61 へ入れるとどう tokenize されるか、`bm25()` が並びを決められるか、`MATCH` 演算子の構文制約。
- 方針（probe 前に確定させる Phase 1 設計）: 1) 連続 CJK に対し 2 文字 / 3 文字の contiguous n-gram を生成し（§1 決定 1.9）、2) ASCII run（長さ 3 以上）を別 group の term とし、3) term を FTS5 専用カラム（正本 Markdown の複製ではなく生成された語彙）へ入れる。README に tokenizer と n-gram の規定は無い（§8.1 L703 / §8.2 L726）。
- 検索 query にも同一の分割関数を適用する（index と query で違う分割を使うと一致しない。README の規定ではなく Phase 1 実装規則）。

### 2.7 HTTP API 面（Phase 1）

#### 2.7.1 README が要求する endpoint（§10.2 L915-921 / §10.3 L927-930）

| Method | Path | README |
| :--- | :--- | :--- |
| GET | `/api/rules` | ルール一覧 L915 |
| POST | `/api/rules` | ルール新規作成 L916 |
| GET | `/api/rules/{id}` | ルール詳細 L917 |
| PUT | `/api/rules/{id}` | ルール更新 L918 |
| GET | `/api/rules/{id}/versions` | バージョン一覧 L919 |
| POST | `/api/rules/{id}/restore` | バージョン復元 L920 |
| GET | `/api/rules/search` | ルール検索 L921 |
| GET | `/api/approvals` | 承認待ち一覧 L927 |
| GET | `/api/approvals/{id}` | 承認対象詳細 L928 |
| POST | `/api/approvals/{id}/approve` | 承認 L929 |
| POST | `/api/approvals/{id}/reject` | 却下 L930 |

README の Rule API / Approval API には `GET /api/rules/{id}/versions/{version}` も `GET /api/approvals/pending` も無い（grep 0 件）。`GET /api/rules/search` L921 には endpoint 名しか無く、query 参数・limit・offset・category/status 参数の規定が無い。README は §10.3 L932「承認APIは認証された人間の操作のみ許可する。」、§12.1 L1029「家庭内ネットワーク限定であっても認証機能を設ける。」・L1031「初期段階ではシンプルなユーザー認証を実装する。」、§17 の table L1406-1421 の L1420 行（項目「認証」、要件「必須」）を要求するが、§10.2・§10.3 の endpoint に認証 scheme は書かれていない。Phase 1 でどう担保するか §2.10.1 Q-02。

#### 2.7.2 Phase 0 契約との差異（docs/plan/2026-11-11 §4 L94-101）

Phase 0 契約 (L95-99) は hub 側で `GET|POST /api/rules`、`GET|PATCH|DELETE /api/rules/{rule_id}`、`POST /api/rules/{rule_id}/approve|reject`、`GET /api/rules/{rule_id}/versions`、`GET /api/rules/conflicts`、`GET /api/approvals?status=`、`POST /api/approvals/{approval_id}/approve|reject`、`POST /api/approvals/resolve-pending` を既に決めている。README §10.2（L915-921）と突き合わせると次が差異。

| 差異 | README | Phase 0 契約 | Phase 1 の扱い |
| :--- | :--- | :--- | :--- |
| path 変数名 | `{id}` L917-920 | `{rule_id}` L95 | 実装を 1 本に寄せる（README 準拠で `{id}`）。Phase 0 契約文書の更新は §2.10.1 Q-07 |
| 更新 verb | `PUT` L918 | `PATCH` L95 | README の `PUT` を実装し、`PATCH` を併用するかは open question Q-07（§2.10.1）で登記 |
| 復元 endpoint | `POST /api/rules/{id}/restore` L920 | 無し | Phase 1 で追加（§1 決定 1.10、差異 D-08） |
| 検索 endpoint | `GET /api/rules/search` L921 | 無し | Phase 1 で追加（§1 決定 1.5・1.7、§2.5） |
| 削除 endpoint | 無し | `DELETE /api/rules/{rule_id}` L95 | README に削除 endpoint は無い。Phase 1 の public API には置かない（tombstone は hub 内部） |

### 2.8 hub ↔ README 適応層

hub 側 vocabulary は Phase 1 でも変更しない（§1 決定 1.14）。README vocabulary を実装 vocabulary とし、hub contract との橋渡しは新しい adapter module 1 か所に閉じる。この契約の執筆時に grep で実測した anchor:

- `backend/app/services/rules.py` L16-18: hub の状態は `pending` / `active` / `tombstoned`、source は `agent` / `family_ui` / `migration`、conflict kind は `intent_duplicate` / `time_overlap`。README の status 5 値（§4.5 L325-329）・`rule_type` 5 値（§4.4 L311-315）・source 区別（§4.3 `created_by` L302 / §11.4 `created_by` L986）とは組目が違う。
- `rules.py` L45 `Rule` は単一の `content` text と `revision` int を持つ。README は front matter の 13 属性（§4.3 L291-303）と `version`（L295）を要求し、本文は §4.2 の見出し構成（L270-284）で保つ。
- `rules.py` L248 `RuleStore` は `create_rule` L288 / `update_rule` L335 / `approve_rule` L368 / `reject_rule` L377 / `delete_rule` L392 を持ち、`update_rule` は status を `pending` へ戻す（L360）、`_transition` L408-424 は active への遷移を pending からのみに限定する（README §4.5 L333 と同じ制約）。README の `draft`・`archived`・`rejected`（L325-329）に対応する hub 状態は無い。
- hub の rules は永続化されない: `chat.py` L203 が `RuleStore()` を生成するのみで、Markdown file 読み書きも SQLite も hub 側に存在しない。`rules_dir`（`config.py` L158）と `database_path`（同 L163、L165 で `data/household.db`）は定義済みだが、rules の読み書きで参照する code は無い（`rules_dir` の出現は `config.py` L158 と `tests/unit/test_config.py` L106 のみ。README §13.4 L1147「`backend/app/main.py` と `tests/live/` が未実装のため起動しない。」）。
- hub AI tool 面も hub vocabulary を使う: `tools.py` L25 `READ_TOOLS = ("read_rules", "detect_rule_conflicts")`、L36 `RuleToolContext` が `store: RuleStore`（L40）を持ち、`_tool_read_rules` L151-158 は hub の status 語で filter する。README vocabulary へ写す変換は adapter 1 か所に閉じ、hub 内部 API を変更しない。例: hub `pending` → README `pending_approval`、hub `revision` → README `rules.current_version`（L974）/ `rule_versions.version`（L983）。

### 2.9 差異記録台帳（README / Phase 0 契約 / hub の実測差分）

README §18 L1435「実装時に公式仕様との差異があれば記録する。」に従う。D-xx はこの契約内で参照する ID。

| ID | 差異 | README | もう一方 | Phase 1 の扱い |
| :--- | :--- | :--- | :--- | :--- |
| D-01 | 判断ログの table 名と HTTP path | `judgment_logs` L1001、列 L1003-1009（version 列なし、`applied_rules_json` L1006）。HTTP は §10.4 History API L934 の `GET /api/history` L938・`GET /api/history/{id}` L939 | Phase 0 契約は `GET /api/history/judgments`（L100）・`POST /api/judgments`（L101） | Phase 1 は rules API のみ実装し、table 名と列名は README を使う。判断作成 endpoint は README 側に不在（grep 0 件）なので §2.10.1 Q-05 で確定 |
| D-02 | 詳細画面の表示項目と正本見出し | §9.4 表示項目 8（L834-841） | §4.2 本文見出し 4（L270/L274/L278/L282、level 1） | 正本見出し 4 節を先行し、UI 側は §11.3（L968-977）と §11.4（L981-988）の値を API projection で補う（open question Q-06）。 |
| D-03 | priority の値域 | 列 L973・属性 L297・例 `priority: normal` L254。値域の列挙なし（grep 0 件） | hub の `Rule`（`rules.py` L48-61）に priority フィールドなし | Phase 1 は `TEXT NOT NULL DEFAULT 'normal'`。許容値は Phase 1 判断で deterministic enum にし、#2 で README 追記を要求 |
| D-04 | `summary` の所在 | 検索出力 key L510。front matter 属性一覧 L291-303・rules 列 L968-977 に無し（grep 0 件） | hub `Rule` は `content` L50 の 1 text のみ | Phase 1 は `summary` を本文から生成する index 派生値とし、front matter へ恒久化しない（#2 で README 化を提案） |
| D-05 | version を指定したルール取得 | §9.4 現在のバージョン L839・§14.3 L1215「既存ルールのバージョンが保持される。」を要求。§6.2 get_rule 入力は `rule_id` L524、§10.2 L915-921 に version 指定 path 無し（grep 0 件） | hub は `revision` int L60 を持つ | Phase 1 は `GET /api/rules/{id}/versions` L919 を実装し、version 指定取得の表現（path か query か）を §2.10.1 Q-04 で確定 |
| D-06 | `draft` / `archived` への遷移条件 | §4.5 L325-329 に列挙のみ。§9.5 L845-854 は承認/却下/復元しか書かない | hub `_transition`（`rules.py` L408-424）には active 化を pending 限定する制約 L420 があるが draft/archived 相当なし | enum と SQL 制約は保持し、Phase 1 の public API では遷移を公開しない。#2 で README 追記を要求 |
| D-07 | README に行の無い必要 endpoint | 無し（`versions/{version}`・`approvals/pending` は grep 0 件）。一覧は L915・L919・L927、検索 L921 には query 規定無し | hub tool 側は status 語で filter する（`tools.py` L151-158） | Phase 1 の query param 設計で補い、README 化すべきものは §2.10.1 Q-01 で #2 へ登記 |
| D-08 | HTTP verb と path 変数 | `PUT /api/rules/{id}` L918・`POST /api/rules/{id}/restore` L920・`{id}` L917-920。削除 endpoint 無し（grep 0 件） | Phase 0 契約 L95 は `PATCH` / `DELETE`・`{rule_id}` | 実装は README 準拠（`PUT`・`restore`・tombstone 露出なし）。Phase 0 契約文書更新は §2.10.1 Q-07 |
| D-09 | version file の命名規約 | 例 L341-347（`v1.md` L344・`v2.md` L345・`v3.md` L346）と L337「ルールの更新時には既存ファイルを上書きせず、変更履歴を残す。」、L349「最新版への参照を管理する。」。規約の明文なし | hub 側は Markdown file を書かない（§2.8 hub 適応層） | Phase 1 は `v<version>.md` を契約とし、README 化は open question Q-01（§2.10.1）で登記 |
| D-10 | 承認時刻列 | `rule_versions.approved_by` L987・`approvals.reviewed_at` L999 のみ。`approved_at` は README 全体に無し（grep 0 件） | hub `Rule` に `approved_at` L58 あり（Markdown 化しない） | Phase 1 は README 列のみ使い、時刻の生成・保存形式と列の追加要否は open question Q-11（§2.10.1）で #2 へ登記 |
| D-11 | README §3 の backend file 名と HEAD の code | `main.py` L136・`api/` L139-143・`services/rule_service.py` L147・`models/rule.py` L161・`repositories/rule_repository.py` L166・`db/` L170-172 | hub 実在は `origin/main` の 17 file（`agent/` 5・`services/` 9・`app/__init__.py`・`app/clock.py`・`app/config.py`）で `api/`・`models/`・`repositories/`・`db/` は 0 件。README 名の新設は Phase 1 の工事（決定 1.16） | README の file 名で Phase 1 module を新設し、hub 側は §1 決定 1.14 で残置（§2.4.4）。hub vocabulary との対応は adapter 局所 |
| D-12 | front matter の YAML literal layout と pinned image の PyYAML 6.0.3 dump 出力 | README §4.2 サンプル L245-268: delimiter L245/L268、group 空行 L249/L252/L255/L258/L262/L265、配列 item 2 space indent L257/L260-L261、unquoted ISO `T` timestamp L263/L264 | 実測: `yaml.dump`（`sort_keys=False`、`allow_unicode=True`）は block sequence item を column 0 へ出す（`indent=2`でも dash indent 0）、`datetime` は `2026-11-07 10:00:00+09:00`（space 区切り）、closing `---` を 含む text の `yaml.load` は `ComposerError: expected a single document in the stream`。Pydantic の dump は datetime を quoted ISO string にする（CJK は escape されない）。`model_dump(mode="python")` の `StrEnum` member を `yaml.dump` へ渡すと safe parser で reject される python tag になる（§2.4.5 実測 9）ため writer は `.value` を scalar へ渡す | §1 決定 1.17 と §2.4.5 の manual writer / hand-slice parser で README の literal を再現し、往復 test を §2.4.5 の配置で固定する |

### 2.10 再建工事（2026-11-19）

Phase 1 として書いていたコードは git reflog に残る `merge origin/main: Fast-forward` で消失した（`reset: moving to a36ff1c` → `merge origin/main: Fast-forward` → HEAD `e681aba`）。消失後に残った Phase 1 の成果物はこの契約書のみなので、以下の基準で作り直す。

- 作業ブランチは `feature/phase1-rule-management`（base は `origin/main` の `e681aba`）。複数の session が同じ worktree を共有するため、`main` への merge・force push は行わない。
- README の基準は HEAD の `README.md`（1519 行）。消失前に読んでいた `a36ff1c` の README（1512 行）との差は §13.4 のゲート表まわりの 15 行だけで、Phase 1 が依赖する §4・§8・§10・§11・§15 の条項は 変化していない。
- 作り直す順序とコミット単位: (1) この契約書 (2) Markdown / YAML Front Matter codec と Pydantic model (§4.2 L242-285、§4.3 L291-303 の 13 属性、§4.4 L305) (3) SQLite schema (4) FTS5 search と tokenizer (5) version 管理と再インデックス (6) rules / approvals / history API と §12.1 の認証 (7) hub ↔ README 適応層。各単位で pinned image の `make lint` / `make test` を gate にする。
- この契約書の引用は 2026-11-19 に README・Phase 0 契約・hub ソースの HEAD へ照らして再検証し、以下の誤りを削除した。
  - README に「安全性と公平性のバランスを評価する。」という文は無い（`公平性` 0 件、`安全性` は §12 見出し L1023「# 12. セキュリティ・安全性」に 1 件）。Phase 1 の完了条件は §15 L1272-1273「ルールを登録・検索・更新できる。」である。
  - README に principle を並べた block は無い（`principles` 0 件、principle 名は grep 0 件）。§2.3 L110-116「AI部分を直接実装しない。」他が書くのは Copilot SDK の抽象化だけである。よって AI が rule vocabulary を選ぶ優先順位は README 側には規定が無い。
  - README に FTS5 の virtual table 名（`rules_fts` 0 件）、token 化手法（`tokenizer` 0 件）、検索 result の上限（`上限` 0 件、`表示件数` 0 件）、AI による「評価」（0 件）の規定は無い。§8.2 の規定は L741「検索結果は関連度順で返す。」、L743「検索対象は原則としてactiveのルールとする。」、L745「pending_approvalのルールは正式な判断根拠として使用しない。」の 3 文だけである。
#### 2.10.1 open question 台帳（Q-01 から。この session で new question を足すときはこの表に行を足す）

README 側に要求行が無く、Phase 1 の実装判断が先に要る論点の一覧（README §18 L1435「実装と仕様の差異は、この README に実装影響を明記する。」と Issue #2 登記先 L25「- [ ] Issue #2: README の Phase 1 仕様を UI 情報を含めて確定」）。

2026-11-19 現在の登記は Q-01 から Q-11 まで。§2.7.2・§2.9 差異台帳・§2.10 の本文が参照する open question ID は、下の table の ID を指す。

| ID | 論点 | Phase 1 先行実装 | #2 へ登記する内容 | 参照 |
| :--- | :--- | :--- | :--- | :--- |
| Q-01 | README に規定の無い要求（category dir slug、`v<n>.md` の file 名、必要な endpoint、時刻列）を README へ追記するか | 契約側で先行する（決定 1.4・決定 1.10・決定 1.12・決定 1.14・決定 1.16） | README §4・§10・§11 への追記 | 差異 D-07・差異 D-09・差異 D-10、§2.4.3、§2.7.1 |
| Q-02 | HTTP の auth scheme をどう担保するか。README §17 L1420 は「認証」row の値が「必須」のみで §10 の endpoint 行に auth の記述が無い | README 準拠の endpoint だけ作り、認証は §12.1 L1029「家庭内ネットワーク限定であっても認証機能を設ける。」と L1031「初期段階ではシンプルなユーザー認証を実装する。」で先行し、scheme は hub `app/services/permissions_api.py` の precedent で決める | README §10 への auth 条項追記 | §2.7.1 |
| Q-03 | 検索 limit の README 側規定（README §8.2 L714-745 に limit の数値は無く、§6.2 例の `"limit": 10`（L496）だけ） | 決定 1.7（既定 10・上限 20 で clamping）で先行実装する | README §8.2 への limit 条項追記 | 決定 1.7、§2.5.2 |
| Q-04 | version を指定した rule 取得と query params（README §9.4 L839・§14.3 L1215 が version 保持を要求、§6.2 `get_rule` 入力は `rule_id` L524 のみ） | Phase 1 は §2.7 の contract endpoint で先行 | README §6.2・§10.2 への行追加 | 差異 D-05 |
| Q-05 | 判断ログ作成 endpoint と tool（README 側に該当行が無い） | Phase 1 では API を作らない | README §6・§10 への行追加 | 差異 D-01、§2.3 |
| Q-06 | `summary` の生成方法（README key であり §4.3・§11 に列が無い）と §9.4 UI 8 項目の projection | `rule_versions.summary` 列（README §11 に無い列）へ §4.2 の 4 見出し（README L270/L274/L278/L282）直後の text row（README L272）を書き、UI 側の 8 項目は API projection で補う | README §4.3・§11.4 への列追加 | 差異 D-02・D-04、§2.4.2、§2.5.3 |
| Q-07 | 更新 verb と path 変数（README `PUT` L918 と `{id}` L917、Phase 0 契約 §4 L95 の `PATCH` と `{rule_id}`） | README の `PUT` と `{id}` を実装し、Phase 0 契約文書は未更新 | Phase 0 契約 §4 の更新 | 差異 D-08、§2.7.2 |
| Q-08 | hub の code 配置が README tree と違うこと（hub は origin/main 時点の `backend/app` 17 file で、`app/services/rules.py`・`app/services/tools.py`・`app/services/chat.py` が hub 名。Phase 1 が新設する `api/`・`db/`・`repositories/`・`services/rule_service.py`・`services/search_service.py`・`services/approval_service.py` は hub に 0 件、`app/models/` も hub 側に 0 件） | hub file は変更せず adapter で橋渡しし（決定 1.14・§2.8）、README §3 tree L136-172 の file 名で Phase 1 module を新設する | README §15 Phase 1 L1258 の「実装内容」L1262-1267 と §3 tree L136-172 の対応表。Phase 1 には numbered tool block が無い（tool 名は Phase 2 L1282-1285 の search_rules・get_rule・propose_rule・evaluate_rule） | 差異 D-11、決定 1.14、§2.4.4、§2.8 |
| Q-09 | hub status の 3 値（`app/services/rules.py` の `RuleStatus = str` L13 の comment と `RULE_STATUSES = ("pending", "active", "tombstoned")` L16、dataclass の既定値 `status: RuleStatus = "pending"` L51）と README status 5 値（§4.5 L325-329）の対応 | README 5 値を正本とし hub 3 値を map する（§2.2 状態遷移表）。hub が README の 5 値を持つ時期は #2 まで保留 | README §4.5 と hub vocabulary の対応表、hub 側の tombstoned の README 名（archived と rejected の区別） | 差異 D-06、§2.2 |
| Q-10 | `pending_approval` の却下操作後の状態と本文の扱い、同一 id の再提案可否（README §9.5 L849・L851、status `rejected` L328） | 却下は `rejected` とし、active へ昇格させない。本文は削除しない | README §4.5・§9.5 への条項追記 | §2.2 |
| Q-11 | 時刻の生成・保存形式。README は §4.2 サンプルの `created_at` L263・`updated_at` L264 が `2026-11-07T10:00:00+09:00`（秒精度 + 固定 offset）で、§17 L1415 が Asia/Tokyo を要求するが、naive な値の解釈、保存精度、UTC 保存の可否、§11 のテーブル L968-1009 の `created_at` / `updated_at` / `reviewed_at` 以外に承認・却下・archive の時刻列を置くかを規定しない | Markdown front matter は README L263/L264 と byte 一致する `datetime.isoformat(timespec="seconds")` 表示に固定（hub `app/clock.py` の `tokyo_iso()` L27-32 は milliseconds 形式なので front matter には使わない）。naive な値は持たず、DB の rule 系列（`rules.created_at` と rule text の `created_at`）は hub の先行例（`app/clock.py` L12-13 の Asia/Tokyo と `+09:00` fallback）に合わせて Asia/Tokyo offset 付き ISO text で書く | README への時刻条項の追記（保存形式、精度、naive 値の扱い、DB に追加する時刻列の名前）、README の front matter sample は datetime 形式のみ（L263/L264）で §4.3 L291-303 の 13 属性に date 専用属性も無いため、date だけの値を認めるかの規定が無い点、§4.3 へ `approved_at` 等の列を追加するか（§2.4.1 で front matter に追加しない方針を決めている） | 差異 D-10、§2.4.1、hub clock precedent（`app/clock.py` L12-32） |
