"""ルール検索の term 分割（決定 1.9・決定 1.21）。

正本 Markdown を index へ入れる側と、search query の両方がこの同一関数を通す。
index 側と query 側で単語の区切り方が違うと、term が一致して検索できなくなるため
（README §8.2 L751）、分割規則は 1 箇所に固定する。

実測 3 が示す通り、n-gram tokenizer と同じ規則は FTS5 の `trigrams` と `tokenchars`
の併用でしか再現できず、決定 1.9 が認めた「application 側 n-gram」はこの単一関数で
両側を揃える形で実装する（実測 §2.6.1）。

- CJK run（決定 1.21 の codepoint-range で決める文字クラス）からは contiguous な
  2-gram と 3-gram を作る。長さ 1 の run はその 1 文字を term にする。
- ASCII run は非英数字で切り、長さ 3 以上だけを lower-case で term にする（決定 1.9）。
- その他（記号・半角カナ等）は term に含めない。
"""

from __future__ import annotations

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
    # FTS5 の既定 tokenizer が `a-b` 等をどう分析するか（実測 6）は、application 側
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
