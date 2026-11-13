"""特化カテゴリ（NTR / ハード系 / 素人 など）の定義と、DMM API の応答の解析。

ネットワークには触れない純粋な処理だけを置く（テストしやすくするため）。
定義は content/categories.json。サイト側（av_hub_mu_plugin.php の AV_HUB_CATS）と slug を揃えること。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

SLUG_RE = re.compile(r"^[a-z0-9-]{2,20}$")
FLOOR_RE = re.compile(r"^[a-z0-9_]{2,30}$")
# サイト側（プラグイン）で URL に使えない語。/av/{slug}/ と衝突する
RESERVED_SLUGS = frozenset({
    "new", "rank", "trend", "hot", "best", "lowest", "sale", "about", "gems", "buzz",
    "log", "pick", "search", "later", "genre", "actress", "maker", "work", "buy", "budget", "oshi",
    "ending", "swipe", "month", "feed",
})
DEFAULT_PATH = Path(__file__).with_name("content") / "categories.json"


@dataclass(frozen=True)
class Category:
    slug: str                        # URL（/av/{slug}/）とランキング保存先のキー
    label: str                       # タブ名・投稿の見出し
    floor: str = "videoa"            # DMM のフロア（videoa=ビデオ / videoc=素人）
    keywords: tuple[str, ...] = ()   # キーワードで集める場合（先頭の語で人気順を取る）
    genre_name: str = ""             # FANZA のジャンル名で集める場合（ジャンルIDは実行時にAPIで引く）
    add_genre: str = ""              # 取り込む作品に、このジャンル名を付け足す（フロアで集める場合に、サイト側の判定語と揃える）
    primary: bool = False            # 従来の特化ジャンル（NTR）。順位は従来の保存先に入る
    hashtags: str = ""               # Bluesky 投稿のハッシュタグ
    n: int | None = None             # 取り込む件数（未指定なら FOCUS_N）
    rank_n: int | None = None        # ランキングとして記録する件数（未指定なら FOCUS_RANK_N）
    min_reviews: int = 0             # レビュー件数がこれ未満の作品は取り込まない（0=制限なし）。素人系など、情報の少ない作品を除くため
    fetch_new: bool = True           # False にすると新着の取り込みを行わない（人気順だけ集める）
    enabled: bool = True             # False にすると、取り込み・ランキング・Bluesky投稿のすべてで、このカテゴリを使わない


class CategoryError(ValueError):
    """categories.json の内容が不正。"""


def _clean_list(v: Any) -> tuple[str, ...]:
    if isinstance(v, str):
        v = [x for x in v.split(",")]
    return tuple(str(x).strip() for x in (v or []) if str(x).strip() and str(x).strip() != "-")


def _as_bool(v: Any, default: bool) -> bool:
    """JSON の true/false だけでなく、"false" や 0 のような書き間違いも意図どおりに読む。"""
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "no", "off", "-")
    return bool(v)


def passes_review_gate(cat: Category, review_count: int | None) -> bool:
    """カテゴリの min_reviews を満たす作品か。レビューが無い（None）作品は 0 件として扱う。"""
    return (review_count or 0) >= cat.min_reviews


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
            raise CategoryError(f"{i}番目: slug「{slug}」はサイトの別ページで使われています。別の名前にしてください。")
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
        except (TypeError, ValueError):
            raise CategoryError(f"{slug}: min_reviews は0以上の整数にしてください。") from None
        if min_reviews < 0:
            raise CategoryError(f"{slug}: min_reviews は0以上の整数にしてください。")
        out.append(Category(
            slug=slug,
            label=label,
            floor=floor,
            keywords=_clean_list(r.get("keywords")),
            genre_name=str(r.get("genre_name", "")).strip(),
            add_genre=str(r.get("add_genre", "")).strip(),
            primary=bool(r.get("primary", False)),
            hashtags=str(r.get("hashtags", "")).strip(),
            n=int(r["n"]) if r.get("n") else None,
            rank_n=min(int(r["rank_n"]), 100) if r.get("rank_n") else None,
            min_reviews=min_reviews,
            fetch_new=_as_bool(r.get("fetch_new"), True),
            enabled=_as_bool(r.get("enabled"), True),
        ))
    if sum(c.primary for c in out) > 1:
        raise CategoryError("primary は1つのカテゴリだけに付けてください。")
    return tuple(out)


def load_categories(path: Path | None = None, focus_keywords_env: str = "") -> tuple[Category, ...]:
    """categories.json を読む。無ければ従来どおり「FOCUS_KEYWORDS だけのNTR特化」にする。

    focus_keywords_env: 環境変数 FOCUS_KEYWORDS。primary のカテゴリの keywords を上書きする。
    「-」だけなら primary のカテゴリを無効にする（従来の仕様）。
    enabled=false のカテゴリは、ここで取り除く（取り込み・ランキング・Bluesky投稿のすべてに渡らない）。
    """
    p = path or DEFAULT_PATH
    if p.exists():
        cats = list(parse_categories(json.loads(p.read_text(encoding="utf-8"))))
        cats = [c for c in cats if c.enabled]      # enabled=false のカテゴリは、以降どの処理にも渡さない
    else:
        cats = [Category(slug="ntr", label="NTR", keywords=("寝取り", "寝取られ", "NTR"), primary=True,
                         hashtags="#NTR #寝取られ")]
    env = focus_keywords_env.strip()
    if env == "-":
        return tuple(c for c in cats if not c.primary)
    if env:
        cats = [replace(c, keywords=_clean_list(env)) if c.primary else c for c in cats]
    return tuple(cats)


# ------------------------------------------------------------ DMM API の応答 ----

def _as_list(v: Any) -> list[Any]:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        # XML 由来の { "item": [...] } 形式にも備える
        inner = v.get("item")
        return inner if isinstance(inner, list) else [v]
    return []


def parse_floor_id(payload: dict[str, Any], floor_code: str, service_code: str = "digital", site_code: str = "FANZA") -> str | None:
    """FloorList の応答から、フロアコード（videoa / videoc など）に対応するフロアIDを探す。"""
    for site in _as_list((payload.get("result") or {}).get("site")):
        if str(site.get("code", "")).upper() != site_code.upper():
            continue
        for svc in _as_list(site.get("service")):
            if svc.get("code") != service_code:
                continue
            for fl in _as_list(svc.get("floor")):
                if fl.get("code") == floor_code and fl.get("id") is not None:
                    return str(fl["id"])
    return None


def parse_genres(payload: dict[str, Any]) -> list[dict[str, str]]:
    """GenreSearch の応答から [{id, name}] を取り出す。"""
    out = []
    for g in _as_list((payload.get("result") or {}).get("genre")):
        gid, name = g.get("genre_id"), str(g.get("name", "")).strip()
        if gid is not None and name:
            out.append({"id": str(gid), "name": name})
    return out


def pick_genre(genres: Iterable[dict[str, str]], wanted: str) -> tuple[str | None, list[str]]:
    """ジャンル名から genre_id を決める。完全一致を優先し、無ければ部分一致が1件だけのときに採用する。

    戻り値: (genre_id または None, 候補の名前一覧)。候補は、決められなかったときの案内に使う。
    """
    gs = list(genres)
    exact = [g for g in gs if g["name"] == wanted]
    if exact:
        return exact[0]["id"], [g["name"] for g in exact]
    core = re.sub(r"[・\s]", "", wanted)
    partial = [g for g in gs if wanted in g["name"] or g["name"] in wanted or (core and core in re.sub(r"[・\s]", "", g["name"]))]
    if len(partial) == 1:
        return partial[0]["id"], [partial[0]["name"]]
    return None, [g["name"] for g in partial][:10]
