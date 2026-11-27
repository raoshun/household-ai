"""ルール検索の term 分割・MATCH 生成・検索実行（決定 1.9・1.21・1.22）。

正本 Markdown を index へ入れる側と、search query の両方がこの同一関数を通す。
index 側と query 側で単語の区切り方が違うと、term が一致して検索できなくなるため
（README §8.2 L751）、分割規則は 1 箇所に固定する。

実測 3 が示す通り、query 側の term は index 側 splitter が作った語彙でなければ当た
りにいかない。よって決定 1.9 が認めた「application 側 n-gram」は、index と query
両側の語彙をこの単一関数で揃える形で実装する（実測 §2.6.1）。

- CJK run（決定 1.21 の codepoint-range で決める文字クラス）からは contiguous な
  2-gram と 3-gram を作る。長さ 1 の run はその 1 文字を term にする。
- ASCII run は非英数字で切り、長さ 3 以上だけを lower-case で term にする（決定 1.9）。
- その他（記号・半角カナ等）は term に含めない。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

# 決定 1.21: CJK 語彙の分析単位を codepoint range で決める。unicodedata の category
# は 長音 `ー` や `々` が Lo を外れるため使わない（実測 2）。
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3005, 0x3007),  # 々〆〇
    (0x3041, 0x3096),  # ひらがな
    (0x30A1, 0x30FA),  # カタカナ
    (0x30FC, 0x30FC),  # 長音 ー（語彙として index する）
    (0x4E00, 0x9FFF),  # CJK 基本漢字
    (0xF900, 0xFAFF),  # CJK 互換漢字
    (0xFF66, 0xFF9D),  # 半角カナ
)


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(start <= code <= end for start, end in _CJK_RANGES)


def _is_ascii_alnum(ch: str) -> bool:
    return ("0" <= ch <= "9") or ("A" <= ch <= "Z") or ("a" <= ch <= "z")


def _cjk_grams(run: str) -> list[str]:
    if len(run) == 1:
        return [run]
    grams = [run[i : i + 2] for i in range(len(run) - 1)]
    grams.extend(run[i : i + 3] for i in range(len(run) - 2))
    return grams


def _ascii_terms(run: str) -> list[str]:
    terms: list[str] = []
    buffer: list[str] = []
    for ch in run:
        if _is_ascii_alnum(ch):
            buffer.append(ch)
        elif buffer:
            terms.append("".join(buffer).lower())
            buffer = []
    if buffer:
        terms.append("".join(buffer).lower())
    return [term for term in terms if len(term) >= 3]


@dataclass(frozen=True)
class SearchTerms:
    """1 つの text から作った index / query 用 term のまとまり。

    cjk_groups / ascii_groups は 実測 11 の実測に基づき、同じ source run から作った
    term を 1 個の OR group にするための保持。AND で繋いだ group の intersection が
    query の意味になる（部分一致 search は group を増やさない）。
    """

    cjk_terms: list[str]
    ascii_terms: list[str]
    cjk_groups: list[list[str]]
    ascii_groups: list[list[str]]

    @property
    def is_empty(self) -> bool:
        return not (self.cjk_terms or self.ascii_terms)


def split_search_text(text: str) -> SearchTerms:
    """text を CJK n-gram term と ASCII 語彙 term に分割する（index・query 共通）。"""
    cjk_terms: list[str] = []
    ascii_terms: list[str] = []
    cjk_groups: list[list[str]] = []
    ascii_groups: list[list[str]] = []
    cjk_run: list[str] = []

    def flush_cjk() -> None:
        if not cjk_run:
            return
        grams = _cjk_grams("".join(cjk_run))
        cjk_terms.extend(grams)
        cjk_groups.append(grams)
        cjk_run.clear()

    ascii_run: list[str] = []

    def flush_ascii() -> None:
        if not ascii_run:
            return
        terms = _ascii_terms("".join(ascii_run))
        ascii_terms.extend(terms)
        ascii_groups.append(terms)
        ascii_run.clear()

    # ASCII run は非英数字の境界で切る（決定 1.21、`[^0-9A-Za-z]+` と同値）。
    # FTS5 の既定 tokenizer が `a-b` 等をどう分析するか（実測 4）は、application 側
    # 分割を採るここでは語彙の作り方には影響しない。
    for ch in text:
        if _is_cjk(ch):
            flush_ascii()
            cjk_run.append(ch)
            continue
        if cjk_run:
            flush_cjk()
        if _is_ascii_alnum(ch):
            ascii_run.append(ch)
            continue
        flush_ascii()
    flush_ascii()
    if cjk_run:
        flush_cjk()

    return SearchTerms(
        cjk_terms=cjk_terms,
        ascii_terms=ascii_terms,
        cjk_groups=[g for g in cjk_groups if g],
        ascii_groups=[g for g in ascii_groups if g],
    )


# 決定 1.7: 検索 limit は既定 10・上限 20 の clamp (20 は Phase 1 の実装判断、Q-03)。
DEFAULT_SEARCH_LIMIT = 10
MAX_SEARCH_LIMIT = 20


def _quote_fts_term(term: str) -> str:
    """FTS5 の phrase として term を double quote で囲む。

    決定 1.21 は term をすべて double quote で囲み、term 内の `"` は 2 連続へ変換する
    と規定する (実測 11-12)。term に `"` が現れることは splitter の語彙上無いが、
    規定どおり 2 連続化を builder 側で保つ。
    """
    return '"' + term.replace('"', '""') + '"'


def build_match_query(terms: SearchTerms) -> str | None:
    """SearchTerms から FTS5 MATCH 文字列を作る (決定 1.21、実測 11-12)。

    group は括弧で囲み、group 内 (同じ source run 由来の語彙) は OR、group 間は AND で
    繋ぐ。実測 11 は CJK n-gram と ascii を混ぜた `(a OR b) AND (c OR d)` 形を受理した。
    term が 1 つも無いと空 MATCH になり FTS5 が syntax error を返すため (実測 12)、
    呼び出し側が SQL へ渡さず空結果に変換できるよう None を返す。
    """
    groups = [g for g in (*terms.cjk_groups, *terms.ascii_groups) if g]
    if not groups:
        return None
    return " AND ".join(
        "(" + " OR ".join(_quote_fts_term(term) for term in group) + ")"
        for group in groups
    )


# 決定 1.21 が実測 17 で確定した検索 SQL の形が基。fts 表は alias を付けず実名で
# 指定し (alias 付きは MATCH/bm25 が失敗、実測 15)、bm25() の重み 3 つは
# (rule_id, cjk_terms, ascii_terms) の順への 0.0, 1.0, 1.0 (実測 9-10)。決定 1.16 で
# category・status は rules 側で絞る (実測 16)。summary のため rule_versions を
# rules.current_version が指す版に限定して join する (決定 1.8・1.22。版の対応付けは
# 実測 probe に無い拡張なので
# tests/unit/test_search_service_query.py が実 schema で検証する)。
_SEARCH_SQL_BASE = (
    "SELECT rules.id, rules.title, rules.category, rules.status,"
    " rule_versions.summary, rules.current_version"
    " FROM rule_search_index"
    " INNER JOIN rules ON rules.id = rule_search_index.rule_id"
    " INNER JOIN rule_versions"
    " ON rule_versions.rule_id = rules.id"
    " AND rule_versions.version = rules.current_version"
    " WHERE rule_search_index MATCH ?"
    " AND rules.status = ?"
    "{category_clause}"
    " ORDER BY bm25(rule_search_index, 0.0, 1.0, 1.0) ASC,"
    " rule_search_index.rule_id ASC"
    " LIMIT ?"
)


def search_rules(
    conn: sqlite3.Connection,
    query: str,
    *,
    status: str = "active",
    category: str | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
) -> list[dict[str, object]]:
    """FTS5 index を検索し、決定 1.8 の 6 key projection の list を返す。

    検索対象 status の既定は active (決定 1.6・README §8.2 L743「原則 active のみの
    検索対象」)。category 指定時は rules との join 側で絞り (実測 16)、limit は
    0〜MAX_SEARCH_LIMIT に clamp する (決定 1.7。SQLite の負の LIMIT は無制限に
    なるため負値も 0 へ落とす)。query が語彙を作らない場合は SQL を実行せずに
    空 list を返す (実測 12)。
    """
    match_query = build_match_query(split_search_text(query))
    if match_query is None:
        return []

    clamped_limit = max(0, min(limit, MAX_SEARCH_LIMIT))
    category_clause = " AND rules.category = ?" if category is not None else ""
    sql = _SEARCH_SQL_BASE.format(category_clause=category_clause)
    params: list[object] = [match_query, status]
    if category is not None:
        params.append(category)
    params.append(clamped_limit)
    return [dict(row) for row in conn.execute(sql, params)]


def refresh_rule_index(
    conn: sqlite3.Connection,
    rule_id: str,
    terms: SearchTerms,
) -> None:
    """1 ルールの FTS 行を DELETE→INSERT の対で置き換える (決定 1.21、実測 7・18)。

    FTS5 には rule_id の一意制約も upsert も無く、INSERT だけだと古い行が残るため
    (実測 7)、rule_id 条件の DELETE 後の INSERT に限定する。cell には
    split_search_text() の term list を空白区切りで入れる。index 語彙と query 語彙を
    同じ splitter に揃えることが一致の前提 (実測 3)。
    """
    conn.execute("DELETE FROM rule_search_index WHERE rule_id = ?", (rule_id,))
    conn.execute(
        "INSERT INTO rule_search_index (rule_id, cjk_terms, ascii_terms)"
        " VALUES (?, ?, ?)",
        (rule_id, " ".join(terms.cjk_terms), " ".join(terms.ascii_terms)),
    )
