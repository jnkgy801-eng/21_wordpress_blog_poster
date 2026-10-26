# -*- coding: utf-8 -*-
"""
未成年を連想させる語を含む作品を、取り込み前に除外するフィルター。

方針:
  - 過小検知（見逃し）より過検知（除外しすぎ）のほうが害が小さいので、迷ったら除外する。
  - 正規化（NFKC・小文字化）した上で正規表現で照合するため、全角/半角ゆれに強い。
  - 追加は歓迎。削除・弱体化は、除外対象がなぜ危険かを理解した上で慎重に。
  - 環境変数 EXTRA_EXCLUDE（カンマ区切りの正規表現）で、運用中に語を追加できる。
    （例: 非同意を連想させる語など、サイト方針で除外したいもの）
"""
from __future__ import annotations

import os
import re
import unicodedata
from typing import Iterable

# 年齢・学校種別・児童性を示す語。
_BASE_PATTERNS: list[str] = [
    r"小学",                       # 小学生 / 小学校
]

_EXTRA = [p.strip() for p in os.environ.get("EXTRA_EXCLUDE", "").split(",") if p.strip()]

try:
    _COMPILED = [re.compile(p) for p in _BASE_PATTERNS + _EXTRA]
except re.error as e:  # 設定ミスで全件素通しになるのを避けるため、起動時に止める
    raise SystemExit(f"❌ 除外パターンの正規表現が不正です: {e}")


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").lower()


def _flatten(fields: Iterable[object]) -> str:
    parts: list[str] = []
    for f in fields:
        if not f:
            continue
        if isinstance(f, (list, tuple, set)):
            parts.extend(str(x) for x in f)
        else:
            parts.append(str(f))
    return " / ".join(parts)


def is_allowed(*fields: object) -> tuple[bool, list[str]]:
    """(取り込んでよいか, マッチしたパターン) を返す。"""
    text = _normalize(_flatten(fields))
    hits = [p.pattern for p in _COMPILED if p.search(text)]
    return (not hits, hits)
