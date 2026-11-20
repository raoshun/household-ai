-- Phase 1 ルール管理マイグレーション 0001: rules / rule_versions / approvals / judgment_logs
-- 根拠: docs/plan/2026-11-18-phase1-rule-management-contract.md
--   契約 §2.1 L107-119: README §11 の rules (§11.3 L968-977) と rule_versions (§11.4 L981-988) の
--     列名をそのまま採用する。rules へ README にない approved_at / archived_at を追加 (L112, 差異 D-13。
--     NULL 可能。承認瞬間・.archive 移動瞬間を service が渡す)。
--   契約 §1 決定 1.20: DB は clock を一切読まない。DDL に CURRENT_TIMESTAMP 等の既定の時刻式を
--     置かず (契約 §2.1 L119)、呼び出し側が渡す aware datetime を UTC microsecond text
--     (isoformat(timespec="microseconds")) として保存する。
--   timestamp CHECK は 3 節 (契約 §2.4.5 実測 13-14: SQLite 3.40.1 の実 INSERT 行列で確定した pattern)。
--     (1) GLOB で YYYY-MM-DDTHH:MM:SS.<6digits>+00:00 の形 (2) date(col) IS NOT NULL で範囲外 (3)
--     date(col) = date(julianday(substr(col, 1, 19))) で calendar 不能を弾く。GLOB だけでは
--     2026-02-30 が通り、julianday へ文字列全体を渡すと 23:59:59.999999 が偽拒否されるため substr を使う。
--     NULL 可能列は col IS NULL OR ( 3 節 ) の形。受理・拒否の実値は tests/unit/test_database.py で検証する。
--   対象範囲: README §11 は「SQLiteに以下のテーブルを作成する。」(L945) で conversations (§11.1 L947)・
--     messages (§11.2 L957) も含む 6 table を作る規定だが、契約 §2.1 L116-117 が Phase 1 と特定する
--     rules・rule_versions・approvals・judgment_logs の 4 table のみを作る。
--   外部キーは rules への 2 本 (rule_versions・approvals の rule_id) のみ。本 repo の実行環境
--     (sqlite 3.40.1) で PRAGMA foreign_keys の既定が off (0) であることを実測しているため、アプリは
--     接続時に app/db/database.py の connect() で PRAGMA foreign_keys = ON を有効化する。
--     per-connection 設定なのでこの file には書かない。ON DELETE 句は契約に記載がないため付けない。

CREATE TABLE rules (
    -- README §11.3 L968-977 の 10 列 + 契約 §2.1 L110-112 の Phase 1 追加 2 列 (approved_at・archived_at)。
    -- README §11 の列には型表記が一切無い (契約 §2.1 L107)。そのため、
    -- timestamp 列以外の型は実装判断 (timestamp 列の TEXT + CHECK は §1 決定 1.20 の指定)。
    id TEXT PRIMARY KEY,  -- <category>-<3桁>（例 parenting-001。契約 §2.1 L122。CHECK は付けず値域の生成はサービス層）
    title TEXT NOT NULL,  -- README §11.3 L969。SQLite は検索 index・承認状態・履歴・メタデータで本文の正本は Markdown (契約 §2.1 L107)。README は列名のみ規定なので NOT NULL は実装判断
    category TEXT NOT NULL CHECK (
        category IN ('parenting', 'education', 'health', 'lifestyle', 'finance', 'general')
    ),  -- 6 slug (契約 §2.1 L122 が README §3 L199-204 の 6 slug と規定)
    status TEXT NOT NULL CHECK (
        status IN ('draft', 'pending_approval', 'active', 'rejected', 'archived')
    ),  -- 5 値 (契約 §2.1 L122 が README §4.5 L325-329 の 5 値と規定)。Phase 1 で遷移を公開しない active→draft/archived も enum と SQL 制約だけ保持する (契約 §2.2 L133・差異 D-06)
    rule_type TEXT NOT NULL CHECK (
        rule_type IN ('strict', 'principle', 'preference', 'guideline', 'exception')
    ),  -- 5 値 (契約 §2.1 L122)
    priority TEXT NOT NULL DEFAULT 'normal',  -- 契約 §2.1 L122 が `TEXT NOT NULL DEFAULT 'normal'` と規定。README に値域の列挙が無いので CHECK は付けない (README §11.3 L973・契約 差異台帳 D-03。許容値は #2 の README 改訂で確定)
    current_version INTEGER NOT NULL,  -- 最新の版は rules.current_version が指す (契約 §2.3 L138・README §11.3 L974)。初期値や 0 を許すかなどの値域規定は契約に無いので CHECK を付けず、承認操作での指し替えは契約 §2.2 L129、昇格管理は service 層
    file_path TEXT NOT NULL,  -- data/rules/ からの相対 (契約 §2.1 L122・README §4.6 L342-346)。path の規約管理は Markdown 層に委ね CHECK を課さない (README に version file 命名の記載無し: 差異 D-09)
    created_at TEXT NOT NULL CHECK (
        created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'
        AND date(created_at) IS NOT NULL
        AND date(created_at) = date(julianday(substr(created_at, 1, 19)))
    ),
    updated_at TEXT NOT NULL CHECK (
        updated_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'
        AND date(updated_at) IS NOT NULL
        AND date(updated_at) = date(julianday(substr(updated_at, 1, 19)))
    ),
    approved_at TEXT CHECK (  -- pending_approval→active の承認瞬間に呼び出し側が渡す (差異 D-13)。DB 側に時計の既定値は置かない (§1 決定 1.20)
        approved_at IS NULL
        OR (
            approved_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'
            AND date(approved_at) IS NOT NULL
            AND date(approved_at) = date(julianday(substr(approved_at, 1, 19)))
        )
    ),
    archived_at TEXT CHECK (  -- .archive 移動瞬間 (差異 D-13)
        archived_at IS NULL
        OR (
            archived_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'
            AND date(archived_at) IS NOT NULL
            AND date(archived_at) = date(julianday(substr(archived_at, 1, 19)))
        )
    )
);

CREATE TABLE rule_versions (
    -- README §11.4 L981-988 の 8 列をそのまま採用 (契約 §2.1 L113-114)。本文の正本は Markdown ファイルで、本文は見出し 4 節 (基本方針・適用条件・例外条件・判断基準) をこの順序で持つ (契約 §2.4.2 L154-159)。
    id INTEGER PRIMARY KEY,  -- README は列名のみで型表記が一切無い (契約 §2.1 L107) ので連番の rowid 別名を使う (実装判断)
    rule_id TEXT NOT NULL REFERENCES rules (id),
    version INTEGER NOT NULL CHECK (version >= 1),  -- 「ルールごとに 1 以上の整数とし、新しいほど大きい値」(契約 §2.3 L137)。復元も新 version で番号は使い回さない (契約 §2.2 L131)
    content_hash TEXT NOT NULL,  -- README §11.4 L984 の列で、契約 (L113) も列名のみ。算出方法も値形式も README・契約の規定が無い (grep で列言及 1 件のみ) ため CHECK を付けず、アルゴリズムは repository / service 層で確定する
    change_reason TEXT,  -- 変更理由 (README §11.4 L985)。契約に必須規定は無いので NULL を許す
    created_by TEXT NOT NULL,  -- 作成者 (README §11.4 L986)。未承認の提案行でも作成者は埋まるため NOT NULL は実装判断
    approved_by TEXT,  -- 承認操作で埋める列 (README §11.4 L987・契約 §2.2 L129)。未承認は NULL
    created_at TEXT NOT NULL CHECK (
        created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'
        AND date(created_at) IS NOT NULL
        AND date(created_at) = date(julianday(substr(created_at, 1, 19)))
    ),
    UNIQUE (rule_id, version)  -- 同一 rules の version 重複・使い回しを防ぐ実装判断 (契約 §2.2 L131「番号は使い回さない」・§2.3 L137「新しいほど大きい値」。UNIQUE 自体の規定は契約・README に無い)
);

CREATE TABLE approvals (
    -- README §11.5 L992-999 の 8 列をそのまま採用。reviewed_by・reviewed_at は README に元からある列 (契約 §2.2 L129)。
    id INTEGER PRIMARY KEY,  -- README は列名のみで型表記が一切無い (契約 §2.1 L107) ので連番の rowid 別名を使う (実装判断)
    rule_id TEXT NOT NULL REFERENCES rules (id),
    status TEXT NOT NULL,  -- 承認 status の値域は README §11.5・契約のどちらにも列挙が無いので CHECK を付けない (open item)。API 層で値を埋める
    proposed_by TEXT NOT NULL,  -- 提案者 (README §11.5 L995)。NOT NULL は実装判断
    reviewed_by TEXT,  -- 承認・却下の審査操作で埋める (README §11.5 L996・契約 §2.2 L129)。未審査は NULL
    review_comment TEXT,  -- 審査コメント (README §11.5 L997)
    created_at TEXT NOT NULL CHECK (
        created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'
        AND date(created_at) IS NOT NULL
        AND date(created_at) = date(julianday(substr(created_at, 1, 19)))
    ),
    reviewed_at TEXT CHECK (  -- 承認・却下操作の時刻。呼び出し側が aware datetime を渡す (§1 決定 1.20)。未審査は NULL
        reviewed_at IS NULL
        OR (
            reviewed_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'
            AND date(reviewed_at) IS NOT NULL
            AND date(reviewed_at) = date(julianday(substr(reviewed_at, 1, 19)))
        )
    )
);

CREATE TABLE judgment_logs (
    -- README §11.6 L1003-1009 の 7 列をそのまま採用。承認・却下専用の列は作らず、却下・保留の時刻もこの table の記録で扱う (差異 D-10。契約 §2.1 L116-117)。
    id INTEGER PRIMARY KEY,  -- README は列名のみで型表記が一切無い (契約 §2.1 L107) ので連番の rowid 別名を使う (実装判断)
    conversation_id TEXT,  -- conversations / messages (README §11.1 L947・§11.2 L957) は Phase 1 の 4 table 範囲外なので FK を張らず TEXT のまま (実装判断)
    message_id TEXT,  -- 上の同じ理由で FK なし
    applied_rules_json TEXT,  -- 判断時に適用したルール。README §11.6 の列に version が無く (差異 D-01)、判断時点の version を控える必要があり (契約 §2.3 L139) JSON 書いた側が version を込ませる。JSON の書式は README・契約に規定が無いので CHECK を付けない
    provider TEXT,  -- 判断に使った LLM provider (README L1007)
    model TEXT,  -- 同上 (README L1008)
    created_at TEXT NOT NULL CHECK (
        created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'
        AND date(created_at) IS NOT NULL
        AND date(created_at) = date(julianday(substr(created_at, 1, 19)))
    )
);
