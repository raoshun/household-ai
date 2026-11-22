-- Phase 1 ルール管理マイグレーション 0002: rule_search_index (FTS5) と rule_versions.summary 列
-- 根拠: docs/plan/2026-11-18-phase1-rule-management-contract.md
--   契約 §1 決定 1.21: index 表は rule_search_index (FTS5・3 列 rule_id UNINDEXED・cjk_terms・
--     ascii_terms) で、tokenize 句は省略し既定の unicode61 に固定する (実測 4)。語彙は
--     app/services/search_service.py の split_search_text() が作った term を投入する (決定 1.9・1.21)。
--     FTS5 に一意制約は無く、refresh は rule_id 条件の DELETE と INSERT の対で行う (実測 7・18)。
--     投入・refresh の呼び出し側 (再 index) は後続の実装単位が担う。
--   契約 §1 決定 1.22 (決定 1.21 末尾の改): 0001 (commit 752651e) が契約 §2.4.1 L148-150・差異 D-04・
--     Q-06 の要求する rule_versions.summary 列を作成していなかったため、ここで NULL 可能な text 列を
--     追加する。ADD COLUMN には既定値も CHECK も付けない (ADD COLUMN は非定数の既定値を取れず、
--     本文 4 見出し直後の 1 行目が無い場合は null のままなので §2.4.1 L150 の「無ければ null」と整合)。
--     列への書き込み (Markdown 本文からの生成) は再 index 側の責任。
--   PRAGMA user_version = 2 は実測 19 が実 file 形で受理した行そのもの。app/db/database.py の
--     apply_migrations() が file 番号で user_version を昇めるため file 内指定は重複だが、同値で無害。

CREATE VIRTUAL TABLE rule_search_index USING fts5(
    rule_id UNINDEXED,
    cjk_terms,
    ascii_terms
);

ALTER TABLE rule_versions ADD COLUMN summary text;

PRAGMA user_version = 2;
