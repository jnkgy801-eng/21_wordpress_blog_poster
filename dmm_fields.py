"""DMM API の応答のうち、購入のきっかけになる情報（配信形式ごとの価格・セール終了日）の解析。

ネットワークには触れない純粋な処理だけを置く（テストしやすくするため）。

  prices.price      … 配信形式のなかでいちばん安い価格（例: 7日間レンタル）。「300〜」のような表記のことがある
  prices.deliveries … 配信形式ごとの価格 [{type, price}]
  campaign          … セール期間 {date_begin, date_end, title}（単独・配列のどちらの形でも返りうる）
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

_NUM = re.compile(r"\d[\d,]*")
_DT = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?")
_TYPE_OK = re.compile(r"[^a-z0-9_]")


def to_int(value: Any) -> int | None:
    """「300〜」「1,980」などから先頭の数字を整数で取る（全部つなげると桁が壊れるため）。"""
    m = _NUM.search(str(value or ""))
    return int(m.group().replace(",", "")) if m else None


def _as_list(v: Any) -> list[Any]:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        inner = v.get("item")      # XML 由来の { "item": [...] } 形式にも備える
        return inner if isinstance(inner, list) else [v]
    return []


def parse_deliveries(prices: dict[str, Any] | None) -> list[dict[str, Any]]:
    """配信形式ごとの価格 [{type, price}]。同じ形式が複数あれば安い方を残し、安い順に並べる。"""
    block = (prices or {}).get("deliveries")
    if isinstance(block, dict):
        block = block.get("delivery", block)
    best: dict[str, int] = {}
    for d in _as_list(block):
        if not isinstance(d, dict):
            continue
        t = _TYPE_OK.sub("", str(d.get("type", "")).strip().lower())[:20]
        p = to_int(d.get("price"))
        if t and p is not None and (t not in best or p < best[t]):
            best[t] = p
    return [{"type": t, "price": p} for t, p in sorted(best.items(), key=lambda kv: (kv[1], kv[0]))]


def _parse_dt(raw: Any) -> datetime | None:
    m = _DT.search(str(raw or ""))
    if not m:
        return None
    y, mo, d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    try:
        return datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None


def parse_campaign(item: dict[str, Any], now: datetime) -> dict[str, str] | None:
    """いま開催中のセールのうち、終了がいちばん近いものを返す（日時は日本時間の文字列）。

    now は日本時間（タイムゾーン無し）。開催前・終了済みのセールは無視する。
    開始日が無いものは、終了日がこれからなら開催中として扱う。
    """
    best: tuple[datetime, dict[str, str]] | None = None
    for c in _as_list(item.get("campaign")):
        if not isinstance(c, dict):
            continue
        end = _parse_dt(c.get("date_end"))
        begin = _parse_dt(c.get("date_begin"))
        if end is None or end <= now or (begin is not None and begin > now):
            continue
        title = re.sub(r"\s+", " ", str(c.get("title") or "")).strip()[:150]
        row = {
            "sale_begin": begin.strftime("%Y-%m-%d %H:%M:%S") if begin else "",
            "sale_end": end.strftime("%Y-%m-%d %H:%M:%S"),
            "sale_title": title,
        }
        if best is None or end < best[0]:
            best = (end, row)
    return best[1] if best else None
