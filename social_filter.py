"""SNS（Bluesky など外部サービス）へ投稿する作品タイトルのガード。

age_filter.py（サイトへの取り込み判定）とは独立した、投稿専用の判定です。
外部プラットフォームは学生・未成年を連想させる表現に厳しく、アカウント停止の原因になるため、
該当しうるタイトルは投稿用の画像・文章から外します（サイト本体の掲載には影響しません）。
誤検知（成人向けの正当な作品が外れること）は許容し、見逃しを避ける側に倒しています。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

# タイトルで伏せ字として使われる記号（NFKC 正規化後の形）
_M = "○●〇◯◎*□■×✕"

_PATTERNS: tuple[str, ...] = (
    # 学校・学生に関する語（「大学生」「女子大生」は成人なので対象外）
    r"(?<!大)学生",
    r"生徒|学園|学校|制服|通学|校則|修学|部活|新入生|教え子|共学",
    # 学校種別・学年（伏せ字入りも含む）: 高校 / 高○校 / 中学 / 小学 など
    rf"[高中小][{_M}]?[校生学]",
    rf"女子?[{_M}]?[校生]",
    rf"[{_M}][校学]",
    # 略称（JK / JC / JS）と、その伏せ字
    r"(?<![a-z])j[kcs](?![a-z])",
    rf"(?<![a-z])j[{_M}]",
    # 年齢・幼さを示す語（0〜17歳、未成年、少女 など）
    r"(?<![0-9])(?:[0-9]|1[0-7])歳",
    r"未成年|未[○●〇◯]年|少女|ロリ|幼女|幼[いな少]|おさな",
)
_RE = re.compile("|".join(f"(?:{p})" for p in _PATTERNS))


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").lower()


def is_postable(title: str) -> bool:
    """SNS に載せてよいタイトルなら True。"""
    return _RE.search(_normalize(title)) is None


def filter_rows(rows: Iterable[dict[str, Any]], key: str = "title") -> list[dict[str, Any]]:
    """作品の行（辞書）の一覧から、投稿に使えないものを除く。"""
    return [r for r in rows if is_postable(str(r.get(key, "")))]
