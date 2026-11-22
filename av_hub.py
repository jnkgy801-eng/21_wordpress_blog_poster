#!/usr/bin/env python3
"""大人ナビ（adult-navi21.com）FANZA動画 → WordPress（AVハブ）運用ツール（単一ファイル）。

  python av_hub.py sync [--mode daily|backfill] [--dry-run]   DMM API → フィルター → WordPress へ取り込み、当日のランキングを保存
  python av_hub.py check                                      WordPress の接続・認証を診断する（作品は送らない）
  python av_hub.py cats                                       content/categories.json を DMM API で検証する（WordPress へは送らない）
  python av_hub.py intro --file CSV [--type genre|actress|maker] [--top N] [--overwrite] [--dry-run]
                                                              ジャンル/女優/メーカーの紹介文を CSV からまとめて登録する
  python av_hub.py bluesky [--dry-run]                        サイトの公開データから画像カードを作り Bluesky に投稿する
  python av_hub.py indexnow                                   更新ページを IndexNow（Bing / Yandex）へ通知する

このスクリプトが DMM / FANZA に送るのは、情報取得用の API（api.dmm.com）への GET だけです。
アフィリエイトリンク（al.dmm.co.jp）には一切アクセスしないため、DMM のレポートのクリック数には影響しません。

環境変数の一覧は README.md を参照してください。
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, NoReturn, TypedDict

import requests
from requests.auth import HTTPBasicAuth

try:  # 画像カード（bluesky）でだけ使う。他のコマンドは Pillow が無くても動く
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    Image = ImageDraw = ImageFont = None  # type: ignore[assignment]

JST = dt.timezone(dt.timedelta(hours=9))
UA = "av-hub/2.0"
ROOT = Path(__file__).resolve().parent
CATEGORIES_PATH = ROOT / "content" / "categories.json"
Item = dict[str, Any]


# ====================================================================== 共通 ====

class ApiError(RuntimeError):
    """外部 API の呼び出しに失敗した。"""


def warn(msg: str) -> None:
    print(msg, file=sys.stderr)


def die(msg: str) -> NoReturn:
    raise SystemExit(f"❌ {msg}")


def env(key: str, default: str = "") -> str:
    """環境変数を取る。GitHub Actions は未設定の vars を空文字で渡すため、空のときも既定値を使う。"""
    return os.environ.get(key, "").strip() or default


def env_int(key: str, default: int, maximum: int | None = None) -> int:
    raw = env(key)
    try:
        value = int(raw) if raw else default
    except ValueError:
        die(f"{key} は整数で指定してください（現在: {raw}）")
    return min(value, maximum) if maximum else value


def require_env(*keys: str) -> dict[str, str]:
    missing = [k for k in keys if not env(k)]
    if missing:
        die(f"環境変数が未設定です: {', '.join(missing)}")
    return {k: env(k) for k in keys}


# ログに APIキーが残らないよう、URL のクエリに入る認証情報を伏せる
_SECRET_RE = re.compile(r"(api_id|affiliate_id)=[^&\s'\"]+")


def redact(text: str) -> str:
    return _SECRET_RE.sub(r"\1=***", text)


HTTP = requests.Session()
HTTP.headers["User-Agent"] = UA
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def call_json(session: requests.Session, method: str, url: str, *, retries: int = 3,
              backoff: float = 2.0, timeout: float = 30, **kwargs: Any) -> Any:
    """JSON を返す HTTP 呼び出し。

    通信エラーと一時的な HTTP エラー（429 / 5xx）だけを待って再試行する。
    認証エラーなどの 4xx は何度送っても直らないので、すぐ失敗にする。
    """
    last = ""
    for attempt in range(1, retries + 1):
        try:
            r = session.request(method, url, timeout=timeout, **kwargs)
        except requests.RequestException as e:
            last = f"通信エラー: {redact(str(e))}"
        else:
            if r.status_code < 300:
                try:
                    return r.json()
                except ValueError:
                    last = "JSON として読めない応答です"
            else:
                last = f"HTTP {r.status_code}: {redact(r.text[:200])}"
                if r.status_code not in RETRY_STATUS:
                    break
        if attempt < retries:
            time.sleep(backoff * attempt)
    raise ApiError(f"{method} {redact(url)} に失敗しました（{last}）")


def as_list(value: Any) -> list[Any]:
    """DMM API は 1 件のとき配列でなく単体で返すことがある。XML 由来の {"item": [...]} 形式にも備える。"""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        inner = value.get("item")
        return inner if isinstance(inner, list) else [value]
    return []


def to_int(value: Any) -> int | None:
    """「300〜」「1,980」などから先頭の数字を整数で取る（全部つなげると桁が壊れるため）。"""
    m = re.search(r"\d[\d,]*", str(value or ""))
    return int(m.group().replace(",", "")) if m else None


# ================================================================ 安全フィルター ====

def _norm(text: str) -> str:
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


# 取り込み前の判定。基本パターンは最小限で、運用中は EXTRA_EXCLUDE（カンマ区切りの正規表現）で語を足す。
# 見逃しより除外しすぎのほうが害が小さいので、迷う語は足す側に倒す。
_INGEST_BASE = (r"小学",)


@lru_cache(maxsize=1)
def _ingest_patterns() -> tuple[re.Pattern[str], ...]:
    extra = [p.strip() for p in env("EXTRA_EXCLUDE").split(",") if p.strip()]
    try:
        return tuple(re.compile(p) for p in (*_INGEST_BASE, *extra))
    except re.error as e:  # 設定ミスのまま動くと全件素通しになるため、起動時に止める
        die(f"EXTRA_EXCLUDE の正規表現が不正です: {e}")


def is_allowed(*fields: object) -> tuple[bool, list[str]]:
    """サイトに取り込んでよいか。戻り値: (可否, マッチしたパターン)。"""
    text = _norm(_flatten(fields))
    hits = [p.pattern for p in _ingest_patterns() if p.search(text)]
    return (not hits, hits)


# SNS 投稿専用の判定（サイト掲載には影響しない）。外部サービスは学生・未成年を連想させる表現に厳しく、
# アカウント停止の原因になるため、誤検知を許容して見逃しを避ける側に倒している。
_MASK_CHARS = "○●〇◯◎*□■×✕"   # タイトルで伏せ字に使われる記号（NFKC 正規化後）
_SOCIAL_PATTERNS = (
    r"(?<!大)学生",   # 「大学生」「女子大生」は成人なので対象外
    r"生徒|学園|学校|制服|通学|校則|修学|部活|新入生|教え子|共学",
    rf"[高中小][{_MASK_CHARS}]?[校生学]",
    rf"女子?[{_MASK_CHARS}]?[校生]",
    rf"[{_MASK_CHARS}][校学]",
    r"(?<![a-z])j[kcs](?![a-z])",
    rf"(?<![a-z])j[{_MASK_CHARS}]",
    r"(?<![0-9])(?:[0-9]|1[0-7])歳",
    r"未成年|未[○●〇◯]年|少女|ロリ|幼女|幼[いな少]|おさな",
)
_SOCIAL_RE = re.compile("|".join(f"(?:{p})" for p in _SOCIAL_PATTERNS))


def is_postable(title: str) -> bool:
    return _SOCIAL_RE.search(_norm(title)) is None


def postable_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if is_postable(str(r.get("title", "")))]


# ================================================================== カテゴリ ====
# 定義は content/categories.json。slug はサイト側（av_hub_mu_plugin.php の AV_HUB_CATS）と揃える。

SLUG_RE = re.compile(r"^[a-z0-9-]{2,20}$")
FLOOR_RE = re.compile(r"^[a-z0-9_]{2,30}$")
# /av/{slug}/ と衝突する、サイト側で使用済みの語
RESERVED_SLUGS = frozenset({
    "new", "rank", "trend", "hot", "best", "lowest", "sale", "about", "gems", "buzz", "log", "pick",
    "search", "later", "genre", "actress", "maker", "work", "buy", "budget", "oshi", "ending",
    "swipe", "month", "feed",
})


@dataclass(frozen=True)
class Category:
    slug: str                        # URL（/av/{slug}/）とランキング保存先のキー
    label: str                       # タブ名・投稿の見出し
    floor: str = "videoa"            # videoa=ビデオ / videoc=素人
    keywords: tuple[str, ...] = ()   # キーワード検索で集める（先頭の語で人気順を取る）
    genre_name: str = ""             # FANZA のジャンル名で集める（IDは実行時に API で引く）
    add_genre: str = ""              # 取り込む作品にこのジャンル名を付け足す（サイト側の判定語と揃える）
    primary: bool = False            # 従来の特化ジャンル（NTR）。順位は従来の保存先に入る
    hashtags: str = ""               # Bluesky 投稿のタグ
    n: int | None = None             # 取り込み件数（未指定なら FOCUS_N）
    rank_n: int | None = None        # ランキングに記録する件数（未指定なら FOCUS_RANK_N）
    min_reviews: int = 0             # レビュー件数がこれ未満の作品は取り込まない（0=制限なし）
    fetch_new: bool = True           # False なら新着を集めず、人気順だけ集める
    enabled: bool = True             # False なら取り込み・ランキング・投稿のすべてで使わない


class CategoryError(ValueError):
    """categories.json の内容が不正。"""


def _clean_list(v: Any) -> tuple[str, ...]:
    if isinstance(v, str):
        v = v.split(",")
    return tuple(s for s in (str(x).strip() for x in (v or [])) if s and s != "-")


def _as_bool(v: Any, default: bool) -> bool:
    """"false" や 0 のような書き間違いも意図どおりに読む。"""
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "no", "off", "-")
    return bool(v)


def parse_categories(data: dict[str, Any]) -> tuple[Category, ...]:
    rows = data.get("categories")
    if not isinstance(rows, list) or not rows:
        raise CategoryError("categories.json に categories（1件以上の配列）がありません。")
    out: list[Category] = []
    seen: set[str] = set()
    for i, r in enumerate(rows, start=1):
        if not isinstance(r, dict):
            raise CategoryError(f"{i}番目のカテゴリがオブジェクトではありません。")
        slug = str(r.get("slug", "")).strip()
        if not SLUG_RE.match(slug):
            raise CategoryError(f"{i}番目: slug「{slug}」は、英小文字・数字・ハイフンの2〜20字にしてください。")
        if slug in RESERVED_SLUGS:
            raise CategoryError(f"{i}番目: slug「{slug}」はサイトの別ページで使われています。")
        if slug in seen:
            raise CategoryError(f"slug「{slug}」が重複しています。")
        seen.add(slug)
        label = str(r.get("label", "")).strip()
        if not label:
            raise CategoryError(f"{slug}: label（タブ名）がありません。")
        floor = str(r.get("floor", "videoa")).strip()
        if not FLOOR_RE.match(floor):
            raise CategoryError(f"{slug}: floor「{floor}」が不正です（例: videoa / videoc）。")
        try:
            min_reviews = int(r.get("min_reviews") or 0)
            n = int(r["n"]) if r.get("n") else None
            rank_n = min(int(r["rank_n"]), 100) if r.get("rank_n") else None
        except (TypeError, ValueError):
            raise CategoryError(f"{slug}: min_reviews / n / rank_n は整数にしてください。") from None
        if min_reviews < 0:
            raise CategoryError(f"{slug}: min_reviews は0以上の整数にしてください。")
        out.append(Category(
            slug=slug, label=label, floor=floor,
            keywords=_clean_list(r.get("keywords")),
            genre_name=str(r.get("genre_name", "")).strip(),
            add_genre=str(r.get("add_genre", "")).strip(),
            primary=bool(r.get("primary", False)),
            hashtags=str(r.get("hashtags", "")).strip(),
            n=n, rank_n=rank_n, min_reviews=min_reviews,
            fetch_new=_as_bool(r.get("fetch_new"), True),
            enabled=_as_bool(r.get("enabled"), True),
        ))
    if sum(c.primary for c in out) > 1:
        raise CategoryError("primary は1つのカテゴリだけに付けてください。")
    return tuple(out)


def load_categories() -> tuple[Category, ...]:
    """有効なカテゴリを返す。環境変数 FOCUS_KEYWORDS は primary の検索語を上書きし、「-」なら primary を止める。"""
    if CATEGORIES_PATH.exists():
        try:
            cats = [c for c in parse_categories(json.loads(CATEGORIES_PATH.read_text(encoding="utf-8"))) if c.enabled]
        except (OSError, json.JSONDecodeError, CategoryError) as e:
            die(f"{CATEGORIES_PATH.name} を読み込めません: {e}")
    else:
        cats = [Category(slug="ntr", label="NTR", keywords=("寝取り", "寝取られ", "NTR"), primary=True, hashtags="#NTR #寝取られ")]
    focus = env("FOCUS_KEYWORDS")
    if focus == "-":
        return tuple(c for c in cats if not c.primary)
    if focus:
        cats = [replace(c, keywords=_clean_list(focus)) if c.primary else c for c in cats]
    return tuple(cats)


def passes_review_gate(cat: Category, review_count: int | None) -> bool:
    """レビューが無い（None）作品は 0 件として扱う。"""
    return (review_count or 0) >= cat.min_reviews


# ============================================================ DMM API 応答の解析 ====
# ネットワークに触れない純粋な関数だけを置く。

_DT_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?")


def parse_floor_id(payload: dict[str, Any], floor_code: str, service_code: str = "digital", site_code: str = "FANZA") -> str | None:
    """FloorList の応答から、フロアコード（videoa / videoc など）に対応するフロアIDを探す。"""
    for site in as_list((payload.get("result") or {}).get("site")):
        if str(site.get("code", "")).upper() != site_code:
            continue
        for svc in as_list(site.get("service")):
            if svc.get("code") != service_code:
                continue
            for fl in as_list(svc.get("floor")):
                if fl.get("code") == floor_code and fl.get("id") is not None:
                    return str(fl["id"])
    return None


def parse_genres(payload: dict[str, Any]) -> list[dict[str, str]]:
    out = []
    for g in as_list((payload.get("result") or {}).get("genre")):
        gid, name = g.get("genre_id"), str(g.get("name", "")).strip()
        if gid is not None and name:
            out.append({"id": str(gid), "name": name})
    return out


def pick_genre(genres: Iterable[dict[str, str]], wanted: str) -> tuple[str | None, list[str]]:
    """ジャンル名から genre_id を決める。完全一致を優先し、無ければ部分一致が1件だけのときに採用する。

    戻り値: (genre_id または None, 候補の名前一覧)。候補は決められなかったときの案内に使う。
    """
    gs = list(genres)
    exact = [g for g in gs if g["name"] == wanted]
    if exact:
        return exact[0]["id"], [g["name"] for g in exact]
    squash = lambda s: re.sub(r"[・\s]", "", s)  # noqa: E731
    core = squash(wanted)
    partial = [g for g in gs if wanted in g["name"] or g["name"] in wanted or (core and core in squash(g["name"]))]
    if len(partial) == 1:
        return partial[0]["id"], [partial[0]["name"]]
    return None, [g["name"] for g in partial][:10]


def parse_deliveries(prices: dict[str, Any] | None) -> list[dict[str, Any]]:
    """配信形式ごとの価格 [{type, price}]。同じ形式が複数あれば安い方を残し、安い順に並べる。"""
    block = (prices or {}).get("deliveries")
    if isinstance(block, dict):
        block = block.get("delivery", block)
    best: dict[str, int] = {}
    for d in as_list(block):
        if not isinstance(d, dict):
            continue
        t = re.sub(r"[^a-z0-9_]", "", str(d.get("type", "")).strip().lower())[:20]
        p = to_int(d.get("price"))
        if t and p is not None and (t not in best or p < best[t]):
            best[t] = p
    return [{"type": t, "price": p} for t, p in sorted(best.items(), key=lambda kv: (kv[1], kv[0]))]


def _parse_dt(raw: Any) -> dt.datetime | None:
    m = _DT_RE.search(str(raw or ""))
    if not m:
        return None
    y, mo, d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    try:
        return dt.datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None


def parse_campaign(item: Item, now: dt.datetime) -> dict[str, str] | None:
    """いま開催中のセールのうち、終了がいちばん近いものを返す。now は日本時間（タイムゾーン無し）。

    開催前・終了済みは無視する。開始日が無いものは、終了日がこれからなら開催中として扱う。
    """
    best: tuple[dt.datetime, dict[str, str]] | None = None
    for c in as_list(item.get("campaign")):
        if not isinstance(c, dict):
            continue
        end, begin = _parse_dt(c.get("date_end")), _parse_dt(c.get("date_begin"))
        if end is None or end <= now or (begin is not None and begin > now):
            continue
        row = {
            "sale_begin": begin.strftime("%Y-%m-%d %H:%M:%S") if begin else "",
            "sale_end": end.strftime("%Y-%m-%d %H:%M:%S"),
            "sale_title": re.sub(r"\s+", " ", str(c.get("title") or "")).strip()[:150],
        }
        if best is None or end < best[0]:
            best = (end, row)
    return best[1] if best else None


# ================================================================= DMM API 取得 ====

class Dmm:
    """DMM（FANZA）アフィリエイト API。ジャンル一覧はフロアごとに1回だけ取得して使い回す。"""

    BASE = "https://api.dmm.com/affiliate/v3"
    PAGE_SIZE = 100      # hits の上限
    SLEEP_SEC = 0.6      # レート制限への配慮

    def __init__(self, api_id: str, affiliate_id: str) -> None:
        self.affiliate_id = affiliate_id
        self._auth = {"api_id": api_id, "affiliate_id": affiliate_id, "output": "json"}
        self._genres: dict[str, list[dict[str, str]]] = {}

    @classmethod
    def from_env(cls) -> Dmm:
        c = require_env("DMM_API_ID", "DMM_AFFILIATE_ID")
        return cls(c["DMM_API_ID"], c["DMM_AFFILIATE_ID"])

    def _get(self, endpoint: str, **params: Any) -> dict[str, Any] | None:
        try:
            body = call_json(HTTP, "GET", f"{self.BASE}/{endpoint}", params={**self._auth, **params}, timeout=20)
        except ApiError as e:
            warn(f"⚠️ DMM API 失敗: {e}")
            return None
        return body if isinstance(body, dict) else None

    def items(self, sort: str, limit: int, *, floor: str = "videoa", gte_date: str | None = None,
              keyword: str | None = None, genre_id: str | None = None) -> list[Item]:
        """人気順（rank）/ 新着順（date）で作品を取得する。途中で失敗したら、取得済みの分を返す。"""
        out: list[Item] = []
        # 未来日の予約作品は購入できず記事価値も低いので、最初から除く
        now = dt.datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S")
        while len(out) < limit:
            params: dict[str, Any] = {
                "site": "FANZA", "service": "digital", "floor": floor, "sort": sort,
                "hits": min(self.PAGE_SIZE, limit - len(out)), "offset": len(out) + 1, "lte_date": now,
            }
            if gte_date:
                params["gte_date"] = gte_date
            if keyword:
                params["keyword"] = keyword
            if genre_id:
                params.update(article="genre", article_id=genre_id)
            body = self._get("ItemList", **params)
            page = ((body or {}).get("result") or {}).get("items") or []
            if isinstance(page, dict):
                page = page.get("item") or []
            if not page:
                break
            out.extend(page)
            time.sleep(self.SLEEP_SEC)
        return out[:limit]

    def genre_id(self, floor_code: str, name: str) -> tuple[str | None, list[str]]:
        """ジャンル名 → ジャンルID。戻り値: (ID または None, 候補名)。"""
        if floor_code not in self._genres:
            self._genres[floor_code] = self._load_genres(floor_code)
        return pick_genre(self._genres[floor_code], name)

    def _load_genres(self, floor_code: str) -> list[dict[str, str]]:
        floor_body = self._get("FloorList")
        floor_id = parse_floor_id(floor_body, floor_code) if floor_body else None
        if not floor_id:
            warn(f"⚠️ フロア「{floor_code}」のIDを API から取得できませんでした。")
            return []
        genres: list[dict[str, str]] = []
        while True:
            body = self._get("GenreSearch", floor_id=floor_id, hits=500, offset=len(genres) + 1)
            batch = parse_genres(body) if body else []
            genres.extend(batch)
            try:
                total = int(((body or {}).get("result") or {}).get("total_count") or len(genres))
            except (TypeError, ValueError):
                total = len(genres)
            if not batch or len(genres) >= total:
                return genres
            time.sleep(self.SLEEP_SEC)


# ==================================================================== 正規化 ====

def _names(value: Any) -> list[str]:
    if isinstance(value, dict):
        value = [value]
    names = [str(v.get("name", "")).strip() for v in (value or []) if isinstance(v, dict)]
    return [n for n in dict.fromkeys(names) if n]


def affiliate_url(item: Item, affiliate_id: str) -> str:
    """計測付きリンクだけを採用する。生URLのままではクリックされても報酬が発生しない。"""
    raw = item.get("affiliateURL") or ""
    if "al.dmm.co.jp" in raw or "al.fanza.co.jp" in raw:
        return raw
    base = raw or item.get("URL", "")
    if not base:
        return ""
    p = urllib.parse.urlsplit(base)
    query = "&".join(kv for kv in p.query.split("&") if kv and not kv.startswith("utm_"))
    clean = urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, query, ""))
    return f"https://al.dmm.co.jp/?lurl={urllib.parse.quote(clean, safe='')}&af_id={affiliate_id}&ch=api&ch_id=link"


def normalize(item: Item, affiliate_id: str, extra_genre: str = "") -> Item | None:
    """API の1件を WordPress 送信用に整える。必須項目が欠けるもの・除外対象は None。"""
    cid = item.get("content_id") or item.get("product_id") or ""
    title = (item.get("title") or "").strip()
    img = item.get("imageURL") or {}
    image = img.get("large") or img.get("list") or img.get("small") or ""
    url = affiliate_url(item, affiliate_id)
    if not (cid and title and image and url):
        return None

    info = item.get("iteminfo") or {}
    genres, actresses = _names(info.get("genre")), _names(info.get("actress"))
    makers, series = _names(info.get("maker")), _names(info.get("series"))

    ok, hits = is_allowed(title, genres, makers, actresses, series)
    if not ok:
        print(f"🛡️ 除外: {cid} ({hits[0]})")
        return None
    if extra_genre and extra_genre not in genres:
        genres = [*genres, extra_genre]   # サイト側がこのジャンル名でカテゴリを判定する

    samples: list[str] = []
    for key in ("sample_l", "sample_s"):
        imgs = ((item.get("sampleImageURL") or {}).get(key) or {}).get("image") or []
        if isinstance(imgs, str):
            imgs = [imgs]
        if imgs:
            samples = [u for u in imgs if u][:12]
            break

    movie_block = item.get("sampleMovieURL") or {}
    movie = next((movie_block[k] for k in ("size_720_480", "size_644_414", "size_560_360", "size_476_306") if movie_block.get(k)), "")

    prices = item.get("prices") or {}
    campaign = parse_campaign(item, dt.datetime.now(JST).replace(tzinfo=None)) or {}
    review = item.get("review") or {}
    try:
        review_avg = float(review.get("average") or 0) or None
        review_count = int(review.get("count") or 0) or None
    except (TypeError, ValueError):
        review_avg = review_count = None

    return {
        "cid": cid, "title": title, "url": url, "image": image, "samples": samples, "movie": movie,
        # prices.price は配信形式のなかで最安の価格（レンタル等を含む）。形式ごとの価格とセール終了日も一緒に送る
        "price": to_int(prices.get("price")),
        "list_price": to_int(prices.get("list_price")),
        "deliveries": parse_deliveries(prices),
        "sale_begin": campaign.get("sale_begin", ""),
        "sale_end": campaign.get("sale_end", ""),
        "sale_title": campaign.get("sale_title", ""),
        "review_avg": review_avg, "review_count": review_count,
        "volume": to_int(item.get("volume")),
        "release_date": (item.get("date") or "")[:10] or None,
        "maker": makers[0] if makers else "",
        "series": series[0] if series else "",
        "genres": genres, "actresses": actresses,
    }


def dedupe(items: list[Item]) -> list[Item]:
    """同じ作品は1件にまとめる（後のデータを採用）。カテゴリ判定用に付けたジャンルは、どちらの分も残す。"""
    merged: dict[str, Item] = {}
    for it in items:
        old = merged.get(it["cid"])
        if old is not None:
            it = {**it, "genres": list(dict.fromkeys([*old.get("genres", []), *it.get("genres", [])]))}
        merged[it["cid"]] = it
    return list(merged.values())


# ================================================================= WordPress ====

class WpClient:
    """AVハブ（mu-plugin）の REST API。認証は管理者ユーザーのアプリケーションパスワード。"""

    BATCH_SIZE = 50   # 1回に送る件数（PHP のタイムアウト対策）

    def __init__(self) -> None:
        c = require_env("WP_URL", "WP_USERNAME", "WP_APP_PASSWORD")
        self.site = c["WP_URL"].rstrip("/")
        self.base = f"{self.site}/wp-json/av/v1"
        self.s = requests.Session()
        self.s.auth = HTTPBasicAuth(c["WP_USERNAME"], c["WP_APP_PASSWORD"])
        self.s.headers["User-Agent"] = UA

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = call_json(self.s, "POST", f"{self.base}/{path}", json=payload, timeout=120, backoff=3)
        if not isinstance(body, dict):
            raise ApiError(f"{path}: 想定外の応答です")
        return body

    def get(self, path: str) -> dict[str, Any] | None:
        """認証つき GET。失敗したら None（旧版プラグインで窓口が無い場合も含む）。"""
        try:
            body = call_json(self.s, "GET", f"{self.base}/{path}", timeout=30, retries=1)
        except ApiError:
            return None
        return body if isinstance(body, dict) else None

    def send_items(self, items: list[Item]) -> int:
        """作品を分割して送る。戻り値: 失敗した件数。"""
        failed = 0
        for i in range(0, len(items), self.BATCH_SIZE):
            batch = items[i:i + self.BATCH_SIZE]
            try:
                res = self.post("ingest", {"items": batch})
                print(f"✅ 取り込み {i + len(batch)}/{len(items)}（保存{res.get('saved')}件）")
            except ApiError as e:
                failed += len(batch)
                warn(f"❌ {e}")
        return failed


# ==================================================================== 診断 ====

def classify_auth(status: int, whoami: dict[str, Any] | None) -> str:
    """認証テストの結果から、失敗の原因と対処を日本語で説明する。whoami は /av/v1/whoami の応答。"""
    if status == 200:
        return "✅ 認証OK。WordPress がID/パスワードを受け取り、管理者として認識しています。"
    if whoami is None:
        return "❌ 認証に失敗し、原因を切り分ける診断窓口も見つかりません（mu-plugin が古い、または未設置）。最新の av_hub_mu_plugin.php を設置してください。"
    if not whoami.get("auth_header"):
        return ("❌ 原因: サーバーが認証ヘッダー（Authorization）を落としています。ID/パスワードは関係ありません。\n"
                "   対処: .htaccess の先頭に次の4行を追加してください。\n"
                "     <IfModule mod_rewrite.c>\n     RewriteEngine On\n"
                "     RewriteRule .* - [E=HTTP_AUTHORIZATION:%{HTTP:Authorization}]\n     </IfModule>")
    err = whoami.get("auth_error") or ""
    if whoami.get("user_id"):
        return "❌ ユーザーは認識できていますが権限が足りません。WP_USERNAME を『管理者』権限のユーザーにしてください。"
    if err == "invalid_username":
        return "❌ 原因: その WP_USERNAME のユーザーがこのサイトに存在しません。プロフィール画面の『ユーザー名』を、そのまま登録してください。"
    if err == "incorrect_password":
        return "❌ 原因: ユーザー名は正しいが、アプリケーションパスワードが違います。このサイトのプロフィールで作り直し、表示された24文字を WP_APP_PASSWORD に登録してください。"
    if err in ("application_passwords_disabled_for_user", "application_passwords_disabled"):
        return "❌ 原因: このユーザー（またはサイト）でアプリケーションパスワードが無効です。セキュリティ系プラグインの設定を確認してください。"
    if whoami.get("app_pw_ok") is False:
        return (f"❌ 原因: このリクエストではアプリケーションパスワードが使えません（HTTPS判定={whoami.get('ssl')}）。"
                "HTTPS と判定されていない場合は、リバースプロキシの設定が考えられます。")
    return (f"❌ 認証ヘッダーは届いていますが、WordPress が認証処理を行っていません（理由コード={err or 'なし'} / HTTPS判定={whoami.get('ssl')}）。\n"
            "   他のプラグインやサーバー設定が REST API の認証に介入している可能性があります。")


def cmd_check(_: argparse.Namespace) -> int:
    """WordPress の接続・認証を、作品を送らずに診断する。"""
    c = require_env("WP_URL", "WP_USERNAME", "WP_APP_PASSWORD")
    base = c["WP_URL"].rstrip("/")
    auth = HTTPBasicAuth(c["WP_USERNAME"], c["WP_APP_PASSWORD"])
    pw_len = len(re.sub(r"\s", "", c["WP_APP_PASSWORD"]))
    print(f"🔎 WP_URL = {base} / ユーザー = {c['WP_USERNAME']} / アプリパスワード = {pw_len}文字（通常24文字）")
    if pw_len != 24:
        print("⚠️ アプリケーションパスワードは通常24文字（スペース除く）です。コピー漏れの可能性があります。")

    def get(path: str, authed: bool = False) -> requests.Response | None:
        try:
            return HTTP.get(f"{base}{path}", timeout=30, auth=auth if authed else None)
        except requests.RequestException as e:
            print(f"❌ 接続失敗 {path}: {e}")
            return None

    r = get("/wp-json/")
    if r is None:
        return 1
    print(f"1) REST API: HTTP {r.status_code} / 最終URL: {r.url}")
    if r.history:
        print(f"⚠️ リダイレクトされています（{r.history[0].url} → {r.url}）。転送時に認証情報が外れるため、WP_URL を最終URLに合わせてください。")
    try:
        has_app_pw = "application-passwords" in (r.json().get("authentication") or {})
    except ValueError:
        has_app_pw = False
    print("2) アプリケーションパスワード機能: " + ("有効" if has_app_pw else "❌ 無効（または REST が応答していません）"))

    r2 = get("/wp-json/av/v1")
    loaded = r2 is not None and r2.status_code == 200
    print("3) AVハブ（mu-plugin）の読み込み: " + ("✅ 読み込まれています" if loaded else f"❌ 見つかりません（HTTP {r2.status_code if r2 is not None else '-'}）"))

    # 取り込みと同じ権限チェック（管理者のみ）を通るかを確認する
    r3 = get("/wp-json/av/v1/ping", authed=True)
    if r3 is None:
        return 1
    try:
        body = r3.json()
    except ValueError:
        body = None
    print(f"4) 認証テスト: HTTP {r3.status_code} / code={body.get('code', '-') if isinstance(body, dict) else '-'}")
    if r3.status_code == 403 and not isinstance(body, dict):
        print("   ⚠️ 応答が WordPress のものではありません。サーバー側で遮断されている可能性があります。")
    whoami: dict[str, Any] | None = None
    r4 = get("/wp-json/av/v1/whoami", authed=True)
    if r4 is not None and r4.status_code == 200:
        try:
            whoami = r4.json()
        except ValueError:
            whoami = None
        w = whoami or {}
        print(f"5) 診断窓口: 認証ヘッダー到達={w.get('auth_header')} / ユーザーID={w.get('user_id')} / "
              f"拒否理由={w.get('auth_error') or '-'} / アプリPW利用可={w.get('app_pw_ok')} / HTTPS判定={w.get('ssl')}")
    print(classify_auth(r3.status_code, whoami))
    return 0 if r3.status_code == 200 else 1


# ==================================================================== 同期 ====

MIN_RANK_RATIO = 0.8   # 人気順の取得がこの割合に満たなければ、欠けたランキングを保存しない


@dataclass(frozen=True)
class SyncConfig:
    rank_top_n: int          # ランキングとして保存する件数
    refresh_n: int           # 価格・レビューを毎日更新する人気上位の件数
    new_days: int            # 新着として遡る日数
    new_max: int             # 新着の最大件数
    backfill_n: int          # backfill の取り込み件数
    focus_n: int             # カテゴリごとの取り込み件数
    focus_backfill_n: int
    focus_rank_n: int

    @classmethod
    def from_env(cls) -> SyncConfig:
        return cls(
            rank_top_n=env_int("RANK_TOP_N", 100, 100), refresh_n=env_int("REFRESH_N", 300),
            new_days=env_int("NEW_DAYS", 14), new_max=env_int("NEW_MAX", 300),
            backfill_n=env_int("BACKFILL_N", 2000), focus_n=env_int("FOCUS_N", 200),
            focus_backfill_n=env_int("FOCUS_BACKFILL_N", 1000), focus_rank_n=env_int("FOCUS_RANK_N", 100, 100),
        )


def collect_category(dmm: Dmm, cfg: SyncConfig, cat: Category, backfill: bool, gte: str
                     ) -> tuple[list[Item], list[dict[str, Any]], bool] | None:
    """カテゴリの作品と、カテゴリ内の人気ランキングを集める。

    戻り値: (取り込む作品, ランキング, ランキングが十分な件数あるか)。ジャンルIDが決められなければ None。
    """
    genre_id: str | None = None
    if cat.genre_name:
        genre_id, candidates = dmm.genre_id(cat.floor, cat.genre_name)
        if not genre_id:
            hint = f" 候補: {' / '.join(candidates)}" if candidates else ""
            warn(f"❌ カテゴリ「{cat.slug}」: ジャンル名「{cat.genre_name}」のIDを特定できません。{hint}\n"
                 "   `python av_hub.py cats` で確認し、content/categories.json の genre_name を直してください。")
            return None
    n = cat.n or (cfg.focus_backfill_n if backfill else cfg.focus_n)
    rank_n = min(cat.rank_n or cfg.focus_rank_n, 100)
    gated = 0   # min_reviews に満たず取り込まなかった件数

    def keep(raw: Item) -> Item | None:
        nonlocal gated
        it = normalize(raw, dmm.affiliate_id, extra_genre=cat.add_genre)
        if it is not None and not passes_review_gate(cat, it.get("review_count")):
            gated += 1
            return None
        return it

    items: list[Item] = []
    ranks: list[dict[str, Any]] = []
    raw_rank = dmm.items("rank", n, floor=cat.floor, keyword=cat.keywords[0] if cat.keywords else None, genre_id=genre_id)
    for pos, raw in enumerate(raw_rank, start=1):
        if (it := keep(raw)) is not None:
            items.append(it)
            if pos <= rank_n:
                ranks.append({"cid": it["cid"], "pos": pos})
    complete = len(raw_rank) >= int(rank_n * MIN_RANK_RATIO)

    if cat.fetch_new:
        new_limit = n if backfill else cfg.new_max
        for kw in (cat.keywords or (None,)):
            for raw in dmm.items("date", new_limit, floor=cat.floor, gte_date=gte, keyword=kw, genre_id=genre_id):
                if (it := keep(raw)) is not None:
                    items.append(it)
    if cat.min_reviews:
        print(f"🧹 {cat.label}: レビュー{cat.min_reviews}件未満のため除外 {gated}件 / 取り込み {len(items)}件")
        if not items and raw_rank:
            warn(f"⚠️ カテゴリ「{cat.slug}」: min_reviews={cat.min_reviews} で全作品が除外されました。"
                 "このフロアの API 応答にレビューが含まれない可能性があります。`cats` で確認してください。")
    return items, ranks, complete


def cmd_sync(args: argparse.Namespace) -> int:
    backfill = args.mode == "backfill"
    wp = None if args.dry_run else WpClient()   # 認証情報の不足は、時間のかかる取得の前に気づけるよう先に確認する
    dmm, cfg, cats = Dmm.from_env(), SyncConfig.from_env(), load_categories()
    now = dt.datetime.now(JST)

    # 1) 人気順。順位は除外前の元の並びで記録する。保存するのは上位 rank_top_n 件だが、
    #    価格・レビュー（最安値の記録）を更新するため、毎日は上位 refresh_n 件まで取り込み直す
    top_n = cfg.backfill_n if backfill else max(cfg.rank_top_n, cfg.refresh_n)
    ranked_raw = dmm.items("rank", top_n)
    # 取得が途中で欠けたランキングを保存すると、翌日の「急上昇」「新規ランクイン」が実態とずれる
    if not backfill and len(ranked_raw) < int(top_n * MIN_RANK_RATIO):
        warn(f"❌ 人気順の取得が {len(ranked_raw)}/{top_n} 件でした（下限 {MIN_RANK_RATIO:.0%}）。API 障害の可能性があるため、保存せず終了します。")
        return 1
    ranks: list[dict[str, Any]] = []
    items: list[Item] = []
    for pos, raw in enumerate(ranked_raw, start=1):
        if (it := normalize(raw, dmm.affiliate_id)) is not None:
            items.append(it)
            if pos <= cfg.rank_top_n:
                ranks.append({"cid": it["cid"], "pos": pos})

    # 2) 新着（発売日の新しい順）
    gte = (now - dt.timedelta(days=90 if backfill else cfg.new_days)).strftime("%Y-%m-%dT00:00:00")
    for raw in dmm.items("date", cfg.backfill_n if backfill else cfg.new_max, gte_date=gte):
        if (it := normalize(raw, dmm.affiliate_id)) is not None:
            items.append(it)

    # 3) 特化カテゴリ。全体の人気上位に入らない作品も集め、カテゴリ内のランキングを別に記録する
    cat_ranks: dict[str, list[dict[str, Any]]] = {}
    degraded = False   # 一部のカテゴリが集められなかった（終了コードを1にして気づけるようにする）
    for cat in cats:
        res = collect_category(dmm, cfg, cat, backfill, gte)
        if res is None:
            degraded = True
            continue
        cat_items, cat_rank, complete = res
        items.extend(cat_items)
        if complete or backfill:
            cat_ranks[cat.slug] = cat_rank
        else:
            warn(f"⚠️ カテゴリ「{cat.slug}」: 人気順の取得が不足しているため、順位は保存しません（作品の取り込みは行います）。")
            degraded = True
        print(f"🎯 {cat.label}（{cat.slug}）: 取り込み {len(cat_items)}件 / ランキング {len(cat_rank)}件")

    items = dedupe(items)
    print(f"📦 取得 {len(ranked_raw)}件（人気）→ 取り込み対象 {len(items)}件 / ランキング {len(ranks)}件")
    if not items:
        warn("❌ 取り込み対象が0件です（API キーや API 障害を確認してください）。")
        return 1
    if wp is None:
        for it in items[:3]:
            print({k: (v if k != "samples" else f"[{len(v)}枚]") for k, v in it.items()})
        return 0

    failed = wp.send_items(items)
    day = now.strftime("%Y-%m-%d")
    try:   # ランキングは作品の取り込み後に送る（WP 側で作品IDに紐づけるため）
        print(f"✅ ランキング保存 {wp.post('rank', {'day': day, 'ranks': ranks}).get('saved')}件（{day}）")
    except ApiError as e:
        warn(f"❌ {e}")
        return 1

    # カテゴリ別ランキング。旧版プラグインに送ると全体のランキングを上書きしかねないため、
    # サイト側がそのカテゴリに対応していることを確かめてから送る
    site_cats = set((wp.get("ping") or {}).get("cats") or [])
    for cat in cats:
        if not (ranks_c := cat_ranks.get(cat.slug)):
            continue
        if not cat.primary and cat.slug not in site_cats:
            warn(f"⚠️ サイト側にカテゴリ「{cat.slug}」がありません（プラグインが古い、または AV_HUB_CATS に未登録）。順位の保存をスキップします。")
            degraded = True
            continue
        payload: dict[str, Any] = {"day": day, "ranks": ranks_c, "cat": cat.slug}
        if cat.primary:
            payload["focus"] = True   # 旧版プラグインでも、従来の特化ランキングに保存される
        try:
            print(f"✅ {cat.label}ランキング保存 {wp.post('rank', payload).get('saved')}件（{day}）")
        except ApiError as e:
            warn(f"❌ {e}")
            return 1
    return 1 if (failed or degraded) else 0


def cmd_cats(_: argparse.Namespace) -> int:
    """content/categories.json の設定を DMM API で確認する（WordPress には送らない）。"""
    dmm, cats = Dmm.from_env(), load_categories()
    bad = 0
    print(f"📋 カテゴリ {len(cats)}件（content/categories.json）")
    for cat in cats:
        print(f"\n■ {cat.label}（/av/{cat.slug}/）floor={cat.floor}" + ("  [primary]" if cat.primary else ""))
        genre_id: str | None = None
        if cat.genre_name:
            genre_id, cands = dmm.genre_id(cat.floor, cat.genre_name)
            if not genre_id:
                print(f"  ❌ ジャンル「{cat.genre_name}」のIDを特定できません。候補: {' / '.join(cands) or 'なし'}")
                bad += 1
                continue
            print(f"  ジャンル「{cat.genre_name}」→ ID {genre_id}（一致: {' / '.join(cands)}）")
        raw = dmm.items("rank", 20, floor=cat.floor, keyword=cat.keywords[0] if cat.keywords else None, genre_id=genre_id)
        if not raw:
            print("  ❌ 作品を取得できませんでした（floor・キーワード・ジャンルの指定を確認してください）。")
            bad += 1
            continue
        ok_items = [k for k in (normalize(r, dmm.affiliate_id, extra_genre=cat.add_genre) for r in raw) if k]
        print(f"  人気上位 {len(raw)}件を取得（年齢フィルター通過 {len(ok_items)}件）")
        for k in ok_items[:3]:
            deli = ", ".join(f"{d['type']}=¥{d['price']:,}" for d in k["deliveries"]) or "（応答に無し）"
            sale = f"〜{k['sale_end'][5:16]}「{k['sale_title'][:20]}」" if k["sale_end"] else "なし"
            print(f"   - {k['title'][:30]}… / ジャンル: {', '.join(k['genres'][:5])}")
            print(f"       最安 ¥{k['price'] or 0:,} / 配信形式の価格: {deli} / セール終了: {sale}")
        n = len(ok_items)
        print(f"  レビューが付いている作品: {sum(1 for k in ok_items if k['review_count'])}/{n}件")
        if cat.min_reviews:
            n_pass = sum(1 for k in ok_items if passes_review_gate(cat, k["review_count"]))
            print(f"  min_reviews={cat.min_reviews} を満たす作品: {n_pass}/{n}件" + ("" if n_pass else "  ⚠️ 0件です。基準を下げるか、レビューの付くフロアか確認してください。"))
        if not cat.fetch_new:
            print("  新着の取り込み: しない（fetch_new=false）")
        n_deli = sum(1 for k in ok_items if k["deliveries"])
        print(f"  配信形式の価格が取れた作品: {n_deli}/{n}件 ／ セール終了日が取れた作品: {sum(1 for k in ok_items if k['sale_end'])}/{n}件")
        if ok_items and not n_deli:
            print("  ⚠️ 応答に配信形式の価格が含まれていません。サイトでは最安の価格だけを表示します。")
    print("\n" + ("✅ すべて取得できました。" if not bad else f"❌ {bad}件のカテゴリに問題があります。"))
    return 1 if bad else 0


# ============================================================== 紹介文CSV取り込み ====
# CSV の規則:
#   ・1行目は見出し。名前の列は ジャンル名 / 女優名 / メーカー名（--name-col で変更可）
#   ・「説明」列 = 紹介文の導入（最初の段落）
#   ・「作品数」列 = 使わない（サイトが件数を自動表示する。--top の並べ替えにだけ使える）
#   ・それ以外の列 = 【列名】値 の行になる。列を足すだけで項目を増やせる（列名は12文字まで）
#   ・文字コードは UTF-8（BOM付き可）か Shift_JIS（Excel の既定）を自動判定

INTRO_NAME_COLS = {"genre": "ジャンル名", "actress": "女優名", "maker": "メーカー名"}
INTRO_BATCH = 100
MAX_LABEL = 12   # 【ラベル】の最大文字数（サイト側の整形ルールに合わせる）


def read_csv(path: Path) -> list[dict[str, str]]:
    try:
        raw = path.read_bytes()
    except OSError as e:
        die(f"CSV を読めません: {e}")
    for enc in ("utf-8-sig", "cp932"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        die("文字コードを判定できません（UTF-8 か Shift_JIS で保存してください）")
    rows = list(csv.DictReader(io.StringIO(text, newline="")))
    if not rows:
        die("CSV にデータ行がありません")
    return [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in rows]


def csv_number(text: str) -> float:
    """「3,015」「3015件」のようなセルから数値を取る。数字が無ければ 0。"""
    m = re.search(r"\d[\d,]*(?:\.\d+)?", text or "")
    return float(m.group().replace(",", "")) if m else 0.0


def build_intro(row: dict[str, str], skip: set[str], lead_col: str) -> str:
    """導入文 + 【ラベル】行 の紹介文を組み立てる（サイト側がこの形式を読みやすく整形する）。"""
    lead = " ".join(row.get(lead_col, "").split())
    if lead and not lead.endswith(("。", "！", "？", ".", "!", "?", "）", ")")):
        lead += "。"
    labeled = [f"【{col}】{' '.join(val.split())}" for col, val in row.items() if col and col not in skip and val.strip()]
    return "\n\n".join(p for p in (lead, "\n".join(labeled)) if p)


def cmd_intro(args: argparse.Namespace) -> int:
    rows = read_csv(Path(args.file))
    name_col = args.name_col or INTRO_NAME_COLS[args.type]
    if name_col not in rows[0]:
        first = next(iter(rows[0]))
        print(f"⚠️ 見出し『{name_col}』が無いため、1列目『{first}』を名前として使います。")
        name_col = first
    ignore = {c.strip() for c in args.ignore.split(",") if c.strip()}

    if args.top < 0:
        die("--top は0以上の整数にしてください")
    if args.top:
        if args.sort_col not in rows[0]:
            die(f"--top の並べ替えに使う列『{args.sort_col}』が CSV にありません（列: {list(rows[0])}）")
        rows = sorted(rows, key=lambda r: csv_number(r.get(args.sort_col, "")), reverse=True)[:args.top]   # 同値は元の並びのまま
        names = "、".join(r.get(name_col, "") for r in rows[:10])
        print(f"🔝 『{args.sort_col}』の多い上位{len(rows)}行だけを対象にします: {names}{'…' if len(rows) > 10 else ''}")

    skip = {name_col, args.lead_col, *ignore}
    labels = [c for c in rows[0] if c and c not in skip]
    if too_long := [c for c in labels if len(c) > MAX_LABEL]:
        die(f"列名が{MAX_LABEL}文字を超えています: {too_long}")
    print(f"📄 {args.file}: {len(rows)}行 / 名前の列=『{name_col}』 導入文=『{args.lead_col}』 ラベル行=『{'』『'.join(labels) or 'なし'}』")

    # 取り込み前の安全確認（未成年を連想させる語・EXTRA_EXCLUDE）と、重複・空欄の除外
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    blocked: list[str] = []
    empty = dup = 0
    for r in rows:
        name, intro = r.get(name_col, ""), build_intro(r, skip, args.lead_col)
        if not name or not intro:
            empty += 1
        elif name in seen:
            dup += 1
        elif not is_allowed(name, intro)[0]:
            blocked.append(name)
        else:
            seen.add(name)
            items.append({"name": name, "intro": intro})
    if blocked:
        print(f"🛡️ 安全フィルターで除外 {len(blocked)}件: {'、'.join(blocked)}")
    if empty or dup:
        print(f"⚠️ 空欄で除外 {empty}件 / 重複で除外 {dup}件")
    if not items:
        die("登録できる行がありません")
    print(f"👀 プレビュー（先頭1件）: 【{items[0]['name']}】\n" + "\n".join("    " + ln for ln in items[0]["intro"].split("\n")))

    wp = WpClient()
    total = {"updated": 0, "would_update": 0, "unchanged": 0}
    not_found: list[str] = []
    skipped: list[str] = []
    failed = 0
    for i in range(0, len(items), INTRO_BATCH):
        batch = items[i:i + INTRO_BATCH]
        try:
            res = wp.post("intros", {"type": args.type, "items": batch, "overwrite": args.overwrite, "dry_run": args.dry_run})
        except ApiError as e:
            failed += len(batch)
            warn(f"❌ {e}")
            continue
        for k in total:
            total[k] += int(res.get(k, 0))
        not_found += res.get("not_found", [])
        skipped += res.get("skipped_existing", [])

    def brief(names: list[str], limit: int) -> str:
        return f"  → {'、'.join(names[:limit])}{'…' if len(names) > limit else ''}" if names else ""

    print(f"\n===== 結果{'（確認のみ・書き込みなし）' if args.dry_run else ''} =====")
    print(f"登録される予定: {total['would_update']}件" if args.dry_run else f"登録した: {total['updated']}件")
    print(f"変更なし（すでに同じ内容）: {total['unchanged']}件")
    print(f"記入済みのため上書きしなかった: {len(skipped)}件{brief(skipped, 30)}")
    print(f"サイトに見つからなかった: {len(not_found)}件{brief(not_found, 40)}")
    if failed:
        warn(f"❌ 送信に失敗: {failed}件")
    return 1 if failed else 0


# ===================================================================== IndexNow ====
# サイトマップ（/av-sitemap.xml）のうち lastmod が「昨日以降」のURLだけを Bing / Yandex に通知する。
# Google は IndexNow 非対応なので、Search Console のサイトマップ送信で行う。
# 準備: ①キー（英数字とハイフンの8〜128字。例 openssl rand -hex 16）を決める
#       ②プラグインの定数 AV_HUB_INDEXNOW_KEY に同じ値を設定（/{キー}.txt が公開される） ③ Variables に INDEXNOW_KEY を登録

INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"
SITEMAP_PATH = "/av-sitemap.xml"
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
INDEXNOW_BATCH = 10_000   # IndexNow の1回あたりの上限
INDEXNOW_HINTS = {
    403: "キーが認識されません。/{キー}.txt がサイトで開けるか確認してください。",
    422: "URL がホストと一致しない、またはキーの形式が不正です。",
    429: "送信が多すぎます。時間をおいてください。",
}


def recent_urls(sitemap_xml: str, since: dt.date) -> list[str]:
    """lastmod が since 以降の URL を重複なしで返す（lastmod の無い URL は対象外）。"""
    urls: list[str] = []
    for u in ET.fromstring(sitemap_xml).findall("sm:url", SITEMAP_NS):
        loc = (u.findtext("sm:loc", default="", namespaces=SITEMAP_NS) or "").strip()
        raw = (u.findtext("sm:lastmod", default="", namespaces=SITEMAP_NS) or "").strip()
        try:
            lastmod = dt.date.fromisoformat(raw[:10]) if raw else None
        except ValueError:
            lastmod = None
        if loc and lastmod and lastmod >= since:
            urls.append(loc)
    return list(dict.fromkeys(urls))


def cmd_indexnow(_: argparse.Namespace) -> int:
    key = env("INDEXNOW_KEY")
    if not key:
        print("ℹ️ INDEXNOW_KEY が未設定のため、IndexNow はスキップします。")
        return 0
    site = require_env("WP_URL")["WP_URL"].rstrip("/")
    try:
        r = HTTP.get(site + SITEMAP_PATH, timeout=30)
        r.raise_for_status()
        since = dt.datetime.now(JST).date() - dt.timedelta(days=1)
        urls = recent_urls(r.text, since)
    except (requests.RequestException, ET.ParseError) as e:
        warn(f"❌ サイトマップを取得・解析できません: {e}")
        return 1
    if not urls:
        print(f"ℹ️ {since} 以降に更新された URL がないため、通知しません。")
        return 0

    ok = True
    for i in range(0, len(urls), INDEXNOW_BATCH):
        chunk = urls[i:i + INDEXNOW_BATCH]
        body = {"host": urllib.parse.urlparse(site).netloc, "key": key, "keyLocation": f"{site}/{key}.txt", "urlList": chunk}
        try:
            r = HTTP.post(INDEXNOW_ENDPOINT, json=body, timeout=30)
        except requests.RequestException as e:
            warn(f"❌ IndexNow への送信に失敗しました: {e}")
            ok = False
            continue
        if r.status_code in (200, 202):
            print(f"✅ IndexNow に {len(chunk)} 件を通知しました（HTTP {r.status_code}）")
        else:
            warn(f"❌ IndexNow がエラーを返しました（HTTP {r.status_code}）{INDEXNOW_HINTS.get(r.status_code, '')}")
            ok = False
    return 0 if ok else 1


# ============================================================== Bluesky 画像カード ====
# サイトの公開データ（/wp-json/av/v1/weekly, /ending）から、文字だけのカードを作る。
# 表紙画像は使わない（画像の権利と、SNS の審査に引っかかる心配を減らすため）。

W, H = 1200, 675
BG, CARD, TEXT, MUTED, GREEN = (17, 19, 24), (28, 31, 39), (236, 238, 243), (150, 156, 170), (74, 222, 128)
WARM, HOT = (251, 191, 36), (248, 113, 113)
FONT_PATHS = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    "C:/Windows/Fonts/meiryo.ttc",
)
BOLD_FONT_PATHS = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
)
MAX_ROWS = 5
ENDING = "ending"   # BSKY_CATS で「もうすぐ終了のセール」を指す語（カテゴリの slug とは衝突しない予約語）
RGB = tuple[int, int, int]


@lru_cache(maxsize=None)
def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for path in (*(BOLD_FONT_PATHS if bold else ()), *FONT_PATHS):
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size, index=0)
            except OSError:
                continue
    die("日本語フォントが見つかりません。Actions では `sudo apt-get install -y fonts-noto-cjk` を先に実行してください。")


def hex_to_rgb(h: str) -> RGB:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (99, 102, 241)
    return (r, g, b)


def mask_words(text: str, words: list[str]) -> str:
    for w in words:
        text = text.replace(w, "○" * len(w))
    return text


def yen(n: int | None) -> str:
    return "—" if n is None else f"¥{n:,}"


def span_of(data: dict[str, Any]) -> int:
    """集計の日数。プラグインが span を返さない場合は 7 日（週間）として扱う。"""
    try:
        return max(1, int(data.get("span") or 7))
    except (TypeError, ValueError):
        return 7


def period_label(data: dict[str, Any]) -> str:
    return "今日" if span_of(data) == 1 else "今週"


def _mmdd(day: Any) -> str:
    return str(day or "")[5:].replace("-", "/")


def _fit(d: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> str:
    """幅に収まるように末尾を … で切る。"""
    if d.textlength(text, font=font) <= max_w:
        return text
    while text and d.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def _rtext(d: ImageDraw.ImageDraw, right_x: float, y: float, text: str, font: ImageFont.FreeTypeFont, fill: RGB) -> None:
    d.text((right_x - d.textlength(text, font=font), y), text, font=font, fill=fill)


def _score(r: dict[str, Any]) -> str:
    return f"★{r['review_avg']:.1f}（{r['review_cnt']}件）" if r.get("review_avg") and r.get("review_cnt") else "レビュー少なめ"


def _list_price_note(r: dict[str, Any]) -> str:
    lp, p = r.get("list_price"), r.get("price")
    return f"・定価{yen(lp)}" if lp and p and lp > p else ""


def _render(title: str, subtitle: str, data: dict[str, Any], rows: list[dict[str, Any]], words: list[str],
            accent: RGB, footer_url: str, left_sub: Any, draw_right: Any) -> Image.Image:
    """全カード共通の描画。行ごとの違い（2行目の文言・右側の表示）だけを left_sub / draw_right で渡す。"""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 10, H], fill=accent)
    d.text((48, 30), data.get("site", ""), font=load_font(22), fill=MUTED)
    d.text((48, 62), title, font=load_font(46, True), fill=TEXT)
    d.text((48, 126), subtitle, font=load_font(22), fill=MUTED)
    f_title, f_num, f_sub = load_font(30, True), load_font(34, True), load_font(21)
    for i, r in enumerate(rows[:MAX_ROWS]):
        y = 176 + i * 90
        d.rounded_rectangle([40, y, W - 40, y + 80], radius=12, fill=CARD)
        d.ellipse([58, y + 12, 108, y + 62], fill=accent)
        num = str(i + 1)
        d.text((83 - d.textlength(num, font=f_num) / 2, y + 17), num, font=f_num, fill=(255, 255, 255))
        d.text((128, y + 8), _fit(d, mask_words(r["title"], words), f_title, 700), font=f_title, fill=TEXT)
        d.text((128, y + 48), left_sub(r), font=f_sub, fill=MUTED)
        draw_right(d, r, y)
    d.text((48, H - 44), footer_url, font=load_font(22), fill=MUTED)
    return img


def rank_card(data: dict[str, Any], rows: list[dict[str, Any]], kind: str, words: list[str], accent: RGB) -> Image.Image:
    """kind: movers=急上昇（順位の変化）/ top=いまの人気。"""
    label, n = data.get("label") or "", min(len(rows), MAX_ROWS)
    if kind == "movers":
        title = f"{period_label(data)}の{label}急上昇 TOP{n}"
        sub = f"{_mmdd(data.get('base_day'))} → {_mmdd(data.get('day'))} の人気順位の変化"
    else:
        title, sub = f"いまの{label}人気 TOP{n}", f"{_mmdd(data.get('day'))} 時点の人気順位"
    f_big, f_sub = load_font(26, True), load_font(21)

    def right(d: ImageDraw.ImageDraw, r: dict[str, Any], y: int) -> None:
        if kind == "movers":
            badge = "NEW IN" if r.get("pos_then") is None else f"↑{r['pos_then'] - r['pos_now']}位"
            _rtext(d, W - 270, y + 10, badge, f_big, GREEN)
            _rtext(d, W - 270, y + 48, f"{r['pos_now']}位", f_sub, MUTED)
        else:
            _rtext(d, W - 270, y + 10, f"{r['pos_now']}位", f_big, GREEN)
        _rtext(d, W - 70, y + 24, yen(r.get("price")), f_big, TEXT)

    return _render(title, sub, data, rows, words, accent, data.get("list_url", data.get("home", "")),
                   lambda r: _score(r) + _list_price_note(r), right)


def drop_card(data: dict[str, Any], rows: list[dict[str, Any]], words: list[str], accent: RGB) -> Image.Image:
    span = span_of(data)
    phrase = "前日から" if span == 1 else f"直近{span}日で"
    f_big, f_sub = load_font(26, True), load_font(21)

    def right(d: ImageDraw.ImageDraw, r: dict[str, Any], y: int) -> None:
        _rtext(d, W - 70, y + 10, f"{yen(r['from_price'])} → {yen(r['price'])}", f_big, TEXT)
        _rtext(d, W - 70, y + 48, f"{yen(r['saved'])} おトク", f_sub, GREEN)

    return _render(f"{period_label(data)}の{data.get('label') or ''}値下がり TOP{min(len(rows), MAX_ROWS)}",
                   f"{phrase}、価格が下がった作品（当サイトの価格記録より）", data, rows, words, accent,
                   data.get("log_url", data.get("home", "")), _score, right)


def ending_card(data: dict[str, Any], rows: list[dict[str, Any]], words: list[str], accent: RGB) -> Image.Image:
    """もうすぐ終了のセール（終了が近い順）。残り時間と、そのときの価格を並べる。"""
    f_big, f_sub = load_font(26, True), load_font(21)

    def right(d: ImageDraw.ImageDraw, r: dict[str, Any], y: int) -> None:
        remain = str(r.get("remain") or "")
        _rtext(d, W - 270, y + 10, remain, f_big, HOT if ("まもなく" in remain or "時間" in remain) else WARM)
        if end_txt := str(r.get("sale_end_text") or ""):
            _rtext(d, W - 270, y + 48, end_txt, f_sub, MUTED)
        _rtext(d, W - 70, y + 24, str(r.get("price_text") or yen(r.get("price"))), f_big, TEXT)

    return _render(f"もうすぐ終了の{data.get('label') or ''}セール TOP{min(len(rows), MAX_ROWS)}",
                   "終了が近い順（終了日時はFANZAの情報・価格は取得時点のもの）", data, rows, words, accent,
                   data.get("list_url", data.get("home", "")), lambda r: _score(r) + _list_price_note(r), right)


# ================================================================ Bluesky 投稿 ====

MAX_GRAPHEMES = 300          # 投稿文の上限
MAX_BLOB_BYTES = 950_000     # 画像1枚の上限（公式は約1MB。余裕を見る）
MAX_ALT_CHARS = 900
VALID_LABELS = {"porn", "sexual", "nudity", "graphic-media"}


class Session(TypedDict):
    did: str
    accessJwt: str


def create_session(service: str, handle: str, app_password: str) -> Session:
    data = call_json(HTTP, "POST", f"{service}/xrpc/com.atproto.server.createSession",
                     json={"identifier": handle, "password": app_password}, retries=1)
    return {"did": data["did"], "accessJwt": data["accessJwt"]}


def encode_image(img: Image.Image) -> tuple[bytes, str]:
    """画像を投稿可能なサイズに収める。PNG が大きすぎれば JPEG にする。"""
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    if buf.tell() <= MAX_BLOB_BYTES:
        return buf.getvalue(), "image/png"
    for quality in (90, 80, 70):
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= MAX_BLOB_BYTES:
            return buf.getvalue(), "image/jpeg"
    raise ApiError("画像が大きすぎて投稿できません。")


_URL_RE = re.compile(r"https?://\S+")
_TAG_RE = re.compile(r"(?<!\S)#[^\s#。、，．,.!?！？]+")


def build_facets(text: str) -> list[dict[str, Any]]:
    """URL とハッシュタグをリンクとして認識させる（位置は UTF-8 のバイト単位で指定する）。"""
    def span(m: re.Match[str]) -> dict[str, int]:
        return {"byteStart": len(text[:m.start()].encode()), "byteEnd": len(text[:m.end()].encode())}

    link, tag = "app.bsky.richtext.facet#link", "app.bsky.richtext.facet#tag"
    return [
        *({"index": span(m), "features": [{"$type": link, "uri": m.group()}]} for m in _URL_RE.finditer(text)),
        *({"index": span(m), "features": [{"$type": tag, "tag": m.group()[1:]}]} for m in _TAG_RE.finditer(text)),
    ]


def fit_text(text: str) -> str:
    """上限を超える場合は、末尾（ハッシュタグ行）から削って収める。"""
    lines = text.split("\n")
    while len("\n".join(lines)) > MAX_GRAPHEMES and len(lines) > 1:
        lines.pop()
    return "\n".join(lines)[:MAX_GRAPHEMES]


def alt_text(heading: str, rows: list[dict[str, Any]], words: list[str]) -> str:
    """画像の代替テキスト（読み上げ・画像が見えない環境向け）。"""
    parts = [f"{i}. {mask_words(str(r['title']), words)}" for i, r in enumerate(rows[:MAX_ROWS], start=1)]
    return f"{heading}。{' / '.join(parts)}"[:MAX_ALT_CHARS]


def post_to_bluesky(text: str, cards: list[tuple[Path, str]], label: str) -> str:
    """画像つきの投稿を送り、投稿の URI を返す。"""
    c = require_env("BSKY_HANDLE", "BSKY_APP_PASSWORD")
    service = env("BSKY_SERVICE", "https://bsky.social").rstrip("/")
    session = create_session(service, c["BSKY_HANDLE"], c["BSKY_APP_PASSWORD"])
    auth = {"Authorization": f"Bearer {session['accessJwt']}"}
    images = []
    for path, alt in cards:
        data, mime = encode_image(Image.open(path))
        blob = call_json(HTTP, "POST", f"{service}/xrpc/com.atproto.repo.uploadBlob",
                         headers={**auth, "Content-Type": mime}, data=data)["blob"]
        images.append({"alt": alt, "image": blob, "aspectRatio": {"width": W, "height": H}})
    record: dict[str, Any] = {
        "$type": "app.bsky.feed.post", "text": text, "langs": ["ja"], "facets": build_facets(text),
        "createdAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        # 成人向けの投稿であることを自己申告する（未設定だとアカウント制限の対象になりうる）
        "labels": {"$type": "com.atproto.label.defs#selfLabels", "values": [{"val": label}]},
        "embed": {"$type": "app.bsky.embed.images", "images": images},
    }
    res = call_json(HTTP, "POST", f"{service}/xrpc/com.atproto.repo.createRecord", headers=auth,
                    json={"repo": session["did"], "collection": "app.bsky.feed.post", "record": record})
    return str(res.get("uri", ""))


@dataclass(frozen=True)
class BskyOptions:
    site: str
    out: Path
    words: list[str]
    accent: RGB
    days: int
    label: str
    hashtags: str
    ending_hashtags: str
    dry_run: bool


def site_json(site: str, path: str, params: dict[str, str]) -> dict[str, Any]:
    body = call_json(HTTP, "GET", f"{site}/wp-json/av/v1/{path}", params=params)
    if not isinstance(body, dict):
        raise ApiError(f"{path}: 想定外の応答です")
    return body


def choose_targets(spec: str, cats: tuple[Category, ...], today: dt.date) -> list[str]:
    """投稿する対象を決める。カテゴリの slug、空文字=サイト全体、ENDING=終了間近のセール。"""
    slugs = [c.slug for c in cats]
    spec = spec.strip()
    if not spec:
        return [*slugs, ENDING] if slugs else ["", ENDING]
    if spec == "rotate":   # 全体と、もうすぐ終了のセールも順番に含める
        pool = [*slugs, "", ENDING]
        return [pool[today.toordinal() % len(pool)]]
    return list(dict.fromkeys("" if t == "all" else t for t in (x.strip() for x in spec.split(",")) if t))


def publish(opt: BskyOptions, name: str, text: str, cards: list[tuple[Path, str]]) -> int:
    """投稿文と代替テキストを out/ に保存し、--dry-run でなければ投稿する。戻り値: 0=成功, 1=失敗。"""
    (opt.out / f"bsky_post_{name}.txt").write_text(text, encoding="utf-8")
    (opt.out / f"bsky_alt_{name}.json").write_text(json.dumps([a for _, a in cards], ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"📝 [{name}] 投稿文:\n{text}")
    for p, _ in cards:
        print(f"🖼  {p}")
    if opt.dry_run:
        print(f"ℹ️ [{name}] --dry-run のため投稿していません。")
        return 0
    try:
        print(f"✅ [{name}] 投稿しました: {post_to_bluesky(text, cards, opt.label)}")
    except (ApiError, KeyError) as e:
        warn(f"❌ [{name}] Bluesky への投稿に失敗しました: {e}")
        return 1
    return 0


def post_target(opt: BskyOptions, slug: str, cats: tuple[Category, ...]) -> int:
    """カテゴリ（slug）またはサイト全体（空文字）の急上昇・値下がりを投稿する。"""
    name = slug or "all"
    cat = next((c for c in cats if c.slug == slug), None)
    params = {"focus": "1" if slug else "0", "days": str(opt.days), **({"cat": slug} if slug else {})}
    try:
        data = site_json(opt.site, "weekly", params)
    except ApiError as e:
        warn(f"❌ [{name}] 週次データを取得できません: {e}（プラグインが v1.5.0 以上か確認してください）")
        return 1
    if slug and data.get("cat") != slug:
        warn(f"⚠️ [{name}] サイト側にこのカテゴリがありません（プラグインが古い、または AV_HUB_CATS に未登録）。スキップします。")
        return 1

    # 外部サービスに載せられないタイトルは、画像に入れる前に除く
    movers, top, drops = (postable_rows(data.get(k) or []) for k in ("movers", "top", "drops"))
    label, period = data.get("label") or "", period_label(data)
    stamp = f"{name}_{dt.datetime.now(JST):%Y%m%d}"
    cards: list[tuple[Path, str]] = []
    has_movers, has_drops, n_main = len(movers) >= 3, len(drops) >= 3, 0
    if has_movers or len(top) >= 3:
        kind, rows = ("movers", movers) if has_movers else ("top", top)
        heading = f"{period}の{label}急上昇ランキング" if has_movers else f"いまの{label}人気ランキング"
        p = opt.out / f"bsky_{kind}_{stamp}.png"
        rank_card(data, rows, kind, opt.words, opt.accent).save(p)
        cards.append((p, alt_text(heading, rows, opt.words)))
        n_main = min(len(rows), MAX_ROWS)
    if has_drops:
        p = opt.out / f"bsky_drops_{stamp}.png"
        drop_card(data, drops, opt.words, opt.accent).save(p)
        cards.append((p, alt_text(f"{period}の{label}値下がりランキング", drops, opt.words)))
    if not cards:
        print(f"ℹ️ [{name}] 投稿できるデータが足りないため、今回は投稿しません。")
        return 0

    if has_movers:
        head = f"【{period}の{label}急上昇TOP{n_main}】"
    elif n_main:
        head = f"【いまの{label}人気TOP{n_main}】"
    else:   # 値下がりだけ投稿できる日
        head = f"【{period}の{label}値下がりTOP{min(len(drops), MAX_ROWS)}】"
    lines = [head, *(["値下がりした作品もまとめました"] if has_drops and n_main else []), "順位の変化と価格の記録はこちら↓",
             data.get("list_url", data.get("home", ""))]
    if tags := (cat.hashtags if cat and cat.hashtags else opt.hashtags):
        lines.append(tags)
    return publish(opt, name, fit_text("\n".join(lines)), cards)


def post_ending(opt: BskyOptions) -> int:
    """もうすぐ終わるセールの投稿。締め切りがあるぶん反応が出やすい。対象が3件未満なら投稿しない。"""
    try:
        data = site_json(opt.site, "ending", {"limit": "5"})
    except ApiError as e:
        warn(f"⚠️ [ending] 終了間近のセールを取得できません: {e}（プラグインが v1.9.0 以上か確認してください）")
        return 0
    rows = postable_rows(data.get("items") or [])
    if len(rows) < 3:
        print("ℹ️ [ending] 終了が近いセールが3件未満のため、今回は投稿しません。")
        return 0
    p = opt.out / f"bsky_ending_{dt.datetime.now(JST):%Y%m%d}.png"
    ending_card(data, rows, opt.words, opt.accent).save(p)
    n, first = min(len(rows), MAX_ROWS), rows[0].get("sale_end_text") or ""
    lines = [f"【もうすぐ終了】セール終了が近い作品 TOP{n}", *([f"いちばん早い終了は {first}"] if first else []),
             "セールが終わると価格が戻ります↓", str(data.get("list_url") or data.get("home") or "")]
    if opt.ending_hashtags:
        lines.append(opt.ending_hashtags)
    return publish(opt, ENDING, fit_text("\n".join(lines)), [(p, alt_text("もうすぐ終了のセール", rows, opt.words))])


def cmd_bluesky(args: argparse.Namespace) -> int:
    if Image is None:
        die("Pillow が必要です: pip install pillow")
    site = require_env("WP_URL")["WP_URL"].rstrip("/")
    label = env("BSKY_LABEL", "porn")
    if label not in VALID_LABELS:
        die(f"BSKY_LABEL は {sorted(VALID_LABELS)} のどれかにしてください（現在: {label}）。")
    if not args.dry_run:
        require_env("BSKY_HANDLE", "BSKY_APP_PASSWORD")   # 画像を作る前に、投稿できる状態か確かめる
    out = Path(env("OUT_DIR", "out"))
    out.mkdir(parents=True, exist_ok=True)
    opt = BskyOptions(
        site=site, out=out, words=[w.strip() for w in env("MASK_WORDS").split(",") if w.strip()],
        accent=hex_to_rgb(env("ACCENT", "#6366f1")), days=env_int("BSKY_DAYS", 7), label=label,
        hashtags=env("HASHTAGS", "#FANZA #AV"), ending_hashtags=env("ENDING_HASHTAGS", "#FANZA #セール"),
        dry_run=args.dry_run,
    )
    cats = load_categories()
    targets = choose_targets(env("BSKY_CATS"), cats, dt.datetime.now(JST).date())
    print(f"🎯 投稿対象: {', '.join(t or 'all' for t in targets)}（比較日数 {opt.days}）")

    failed = 0
    for k, slug in enumerate(targets):
        if k:
            time.sleep(5)   # 続けて投稿しすぎない
        failed += post_ending(opt) if slug == ENDING else post_target(opt, slug, cats)
    return 1 if failed else 0


# ======================================================================== CLI ====

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="av_hub.py", description="FANZA動画 → WordPress（AVハブ）運用ツール")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sync", help="DMM API から取り込み、当日のランキングを保存する")
    p.add_argument("--mode", choices=("daily", "backfill"), default="daily", help="backfill=初回の大量取り込み（1回だけ）")
    p.add_argument("--dry-run", action="store_true", help="WordPress へ送らず、取得とフィルターだけ確認する")
    p.set_defaults(func=cmd_sync)

    sub.add_parser("check", help="WordPress の接続・認証を診断する").set_defaults(func=cmd_check)
    sub.add_parser("cats", help="categories.json を DMM API で検証する").set_defaults(func=cmd_cats)

    p = sub.add_parser("intro", help="紹介文 CSV をまとめて登録する")
    p.add_argument("--file", required=True, help="CSV のパス")
    p.add_argument("--type", choices=tuple(INTRO_NAME_COLS), default="genre")
    p.add_argument("--name-col", help="名前の列の見出し（既定: 種類に応じて ジャンル名 / 女優名 / メーカー名）")
    p.add_argument("--lead-col", default="説明", help="導入文にする列")
    p.add_argument("--ignore", default="作品数", help="使わない列（カンマ区切り）")
    p.add_argument("--top", type=int, default=0, help="--sort-col の数値が大きい順に上位N行だけ登録する（0=全行）")
    p.add_argument("--sort-col", default="作品数", help="--top の並べ替えに使う列")
    p.add_argument("--overwrite", action="store_true", help="記入済みの紹介文も上書きする")
    p.add_argument("--dry-run", action="store_true", help="照合結果だけ確認し、書き込まない")
    p.set_defaults(func=cmd_intro)

    p = sub.add_parser("bluesky", help="画像カードを作って Bluesky に投稿する")
    p.add_argument("--dry-run", action="store_true", help="画像と文章を out/ に作るだけで、投稿しない")
    p.set_defaults(func=cmd_bluesky)

    sub.add_parser("indexnow", help="更新ページを IndexNow に通知する").set_defaults(func=cmd_indexnow)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
