# -*- coding: utf-8 -*-
"""
FANZA動画（AV）→ WordPress（AVハブ）同期スクリプト。GitHub Actions から実行する。

  python av_sync.py --mode daily      # 毎日: 人気上位 + 直近の新着を取り込み、当日のランキングを保存
  python av_sync.py --mode backfill   # 初回: 人気順で多めに取り込む（カタログの土台づくり）
  python av_sync.py --mode daily --dry-run   # 取得とフィルターだけ確認（WPには送らない）
  python av_sync.py --mode cats       # 特化カテゴリ（content/categories.json）の設定をDMM APIで確認する（WPには送らない）

必須の環境変数:
  DMM_API_ID / DMM_AFFILIATE_ID
  WP_URL / WP_USERNAME / WP_APP_PASSWORD   （WP側はアプリケーションパスワード。管理者権限のユーザー）
任意:
  RANK_TOP_N(既定100) REFRESH_N(既定300) NEW_DAYS(既定14) NEW_MAX(既定300) BACKFILL_N(既定2000) EXTRA_EXCLUDE
  特化カテゴリ（NTR / ハード系 / 素人 など）は content/categories.json で定義する。カテゴリごとに、集めた作品と、カテゴリ内の人気ランキングを記録する。
  FOCUS_KEYWORDS(従来の特化ジャンル=primary の語を上書き。「-」にすると primary のカテゴリを取得しない)
  FOCUS_N(既定200) FOCUS_BACKFILL_N(既定1000) FOCUS_RANK_N(既定100)  ※カテゴリごとの n / rank_n で個別に変えられる
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

from age_filter import is_allowed
from categories import Category, load_categories, parse_floor_id, parse_genres, pick_genre

JST = dt.timezone(dt.timedelta(hours=9))
DMM_API = "https://api.dmm.com/affiliate/v3/ItemList"
DMM_FLOOR_API = "https://api.dmm.com/affiliate/v3/FloorList"
DMM_GENRE_API = "https://api.dmm.com/affiliate/v3/GenreSearch"
PAGE_SIZE = 100          # DMM API の hits 上限
BATCH_SIZE = 50          # WPへ1回に送る件数（PHPのタイムアウト対策）
API_SLEEP_SEC = 0.6      # API負荷とレート制限への配慮
Item = dict[str, Any]


@dataclass(frozen=True)
class Config:
    dmm_api_id: str
    dmm_affiliate_id: str
    wp_url: str
    wp_user: str
    wp_password: str
    rank_top_n: int
    new_days: int
    new_max: int
    backfill_n: int
    refresh_n: int
    categories: tuple[Category, ...]
    focus_n: int
    focus_backfill_n: int
    focus_rank_n: int


def load_config(need_wp: bool, need_dmm: bool = True) -> Config:
    # Secrets の貼り付け時に混ざりやすい前後の空白・改行は除去する
    # GitHub Actions は未設定の vars を「空文字」で渡す。空のときも既定値 d を使う
    env = lambda k, d="": os.environ.get(k, "").strip() or d  # noqa: E731
    missing = [k for k in ("DMM_API_ID", "DMM_AFFILIATE_ID") if need_dmm and not env(k)]
    if need_wp:
        missing += [k for k in ("WP_URL", "WP_USERNAME", "WP_APP_PASSWORD") if not env(k)]
    if missing:
        raise SystemExit(f"❌ 環境変数が未設定です: {', '.join(missing)}")
    return Config(
        dmm_api_id=env("DMM_API_ID", ""),
        dmm_affiliate_id=env("DMM_AFFILIATE_ID", ""),
        wp_url=env("WP_URL", "").rstrip("/"),
        wp_user=env("WP_USERNAME", ""),
        wp_password=env("WP_APP_PASSWORD", ""),
        rank_top_n=min(int(env("RANK_TOP_N", "100")), 100),
        new_days=int(env("NEW_DAYS", "14")),
        new_max=int(env("NEW_MAX", "300")),
        backfill_n=int(env("BACKFILL_N", "2000")),
        refresh_n=int(env("REFRESH_N", "300")),
        categories=load_categories(focus_keywords_env=env("FOCUS_KEYWORDS")),
        focus_n=int(env("FOCUS_N", "200")),
        focus_backfill_n=int(env("FOCUS_BACKFILL_N", "1000")),
        focus_rank_n=min(int(env("FOCUS_RANK_N", "100")), 100),
    )


# ---------------------------------------------------------------- 取得 ----

def fetch_items(cfg: Config, sort: str, limit: int, gte_date: str | None = None,
                keyword: str | None = None, floor: str = "videoa", article_id: str | None = None) -> list[Item]:
    """FANZA動画をページングで取得する。途中で失敗した場合は取得済み分を返す。"""
    out: list[Item] = []
    offset = 1
    while len(out) < limit:
        params = {
            "api_id": cfg.dmm_api_id,
            "affiliate_id": cfg.dmm_affiliate_id,
            "site": "FANZA",
            "service": "digital",
            "floor": floor,
            "hits": min(PAGE_SIZE, limit - len(out)),
            "offset": offset,
            "sort": sort,
            "output": "json",
            # 未来日の予約作品は、購入できず記事価値も低いので最初から除外する
            "lte_date": dt.datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S"),
        }
        if gte_date:
            params["gte_date"] = gte_date
        if keyword:
            params["keyword"] = keyword
        if article_id:   # ジャンルIDでの絞り込み
            params["article"] = "genre"
            params["article_id"] = article_id
        items = _get_page(params)
        if not items:
            break
        out.extend(items)
        offset += len(items)
        time.sleep(API_SLEEP_SEC)
    return out[:limit]


def _get_page(params: dict[str, Any], retries: int = 3) -> list[Item]:
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(DMM_API, params=params, timeout=20)
            r.raise_for_status()
            body = r.json().get("result", {})
            items = body.get("items", [])
            if isinstance(items, dict):
                items = items.get("item", [])
            return items or []
        except (requests.RequestException, ValueError) as e:
            print(f"⚠️ DMM API失敗 (offset={params['offset']}, {attempt}/{retries}): {e}", file=sys.stderr)
            time.sleep(2 * attempt)
    return []


def _get_json(url: str, params: dict[str, Any], retries: int = 3) -> dict[str, Any] | None:
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            body = r.json()
            return body if isinstance(body, dict) else None
        except (requests.RequestException, ValueError) as e:
            print(f"⚠️ DMM API失敗 ({url.rsplit('/', 1)[-1]}, {attempt}/{retries}): {e}", file=sys.stderr)
            time.sleep(2 * attempt)
    return None


def _auth_params(cfg: Config) -> dict[str, Any]:
    return {"api_id": cfg.dmm_api_id, "affiliate_id": cfg.dmm_affiliate_id, "output": "json"}


def resolve_floor_id(cfg: Config, floor_code: str) -> str | None:
    """フロアコード（videoa / videoc）→ フロアID。ジャンル検索に必要。"""
    body = _get_json(DMM_FLOOR_API, _auth_params(cfg))
    return parse_floor_id(body, floor_code) if body else None


def resolve_genre_id(cfg: Config, floor_code: str, genre_name: str) -> tuple[str | None, list[str]]:
    """ジャンル名 → ジャンルID（FANZAのジャンル一覧から探す）。戻り値: (ID または None, 候補名)。"""
    floor_id = resolve_floor_id(cfg, floor_code)
    if not floor_id:
        print(f"⚠️ フロア「{floor_code}」のIDをAPIから取得できませんでした。", file=sys.stderr)
        return None, []
    genres: list[dict[str, str]] = []
    offset = 1
    while True:
        body = _get_json(DMM_GENRE_API, {**_auth_params(cfg), "floor_id": floor_id, "hits": 500, "offset": offset})
        batch = parse_genres(body) if body else []
        genres.extend(batch)
        try:
            total = int(((body or {}).get("result") or {}).get("total_count") or len(genres))
        except (TypeError, ValueError):
            total = len(genres)
        if not batch or len(genres) >= total:
            break
        offset += len(batch)
        time.sleep(API_SLEEP_SEC)
    return pick_genre(genres, genre_name)


def collect_category(cfg: Config, cat: Category, mode: str, gte: str, min_ratio: float
                     ) -> tuple[list[Item], list[dict[str, Any]], bool] | None:
    """カテゴリの作品と、カテゴリ内の人気ランキングを集める。

    戻り値: (取り込む作品, ランキング, ランキングが十分な件数あるか)。ジャンルIDが決められないなど、集められなければ None。
    """
    article_id: str | None = None
    if cat.genre_name:
        article_id, candidates = resolve_genre_id(cfg, cat.floor, cat.genre_name)
        if not article_id:
            hint = f" 候補: {' / '.join(candidates)}" if candidates else ""
            print(f"❌ カテゴリ「{cat.slug}」: ジャンル名「{cat.genre_name}」のIDを特定できません。{hint}\n"
                  "   `python av_sync.py --mode cats` で確認し、content/categories.json の genre_name を直してください。", file=sys.stderr)
            return None
    n = cat.n or (cfg.focus_backfill_n if mode == "backfill" else cfg.focus_n)
    rank_n = min(cat.rank_n or cfg.focus_rank_n, 100)
    new_limit = n if mode == "backfill" else cfg.new_max
    kw0 = cat.keywords[0] if cat.keywords else None

    items: list[Item] = []
    ranks: list[dict[str, Any]] = []
    raw_rank = fetch_items(cfg, "rank", n, keyword=kw0, floor=cat.floor, article_id=article_id)
    for pos, raw in enumerate(raw_rank, start=1):
        it = normalize(raw, cfg, extra_genre=cat.add_genre)
        if it is None:
            continue
        items.append(it)
        if pos <= rank_n:
            ranks.append({"cid": it["cid"], "pos": pos})
    complete = len(raw_rank) >= int(rank_n * min_ratio)
    for kw in (cat.keywords or (None,)):
        for raw in fetch_items(cfg, "date", new_limit, gte_date=gte, keyword=kw, floor=cat.floor, article_id=article_id):
            it = normalize(raw, cfg, extra_genre=cat.add_genre)
            if it is not None:
                items.append(it)
    return items, ranks, complete


# ---------------------------------------------------------------- 正規化 ----

def _yen(value: Any) -> int | None:
    # 「300〜」のような表記でも先頭の数字だけを取る（数字を全部連結すると桁が壊れるため）
    m = re.search(r"\d[\d,]*", str(value or ""))
    return int(m.group().replace(",", "")) if m else None


def _names(value: Any) -> list[str]:
    if isinstance(value, dict):
        value = [value]
    names = [str(v.get("name", "")).strip() for v in (value or []) if isinstance(v, dict)]
    return [n for n in dict.fromkeys(names) if n]


def _affiliate_url(item: Item, affiliate_id: str) -> str:
    """計測付きリンクだけを採用する。生URLのままだとクリックされても報酬が発生しない。"""
    raw = item.get("affiliateURL") or ""
    if "al.dmm.co.jp" in raw or "al.fanza.co.jp" in raw:
        return raw
    base = raw or item.get("URL", "")
    if not base:
        return ""
    p = urllib.parse.urlsplit(base)
    query = "&".join(kv for kv in p.query.split("&") if kv and not kv.startswith("utm_"))
    clean = urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, query, ""))
    return (
        "https://al.dmm.co.jp/?lurl=" + urllib.parse.quote(clean, safe="")
        + f"&af_id={affiliate_id}&ch=api&ch_id=link"
    )


def normalize(item: Item, cfg: Config, extra_genre: str = "") -> Item | None:
    """APIの1件をWP送信用に整える。必須項目が欠けるもの・除外対象は None。"""
    cid = item.get("content_id") or item.get("product_id") or ""
    title = (item.get("title") or "").strip()
    info = item.get("iteminfo") or {}
    img = item.get("imageURL") or {}
    image = img.get("large") or img.get("list") or img.get("small") or ""
    url = _affiliate_url(item, cfg.dmm_affiliate_id)
    if not (cid and title and image and url):
        return None

    genres = _names(info.get("genre"))
    actresses = _names(info.get("actress"))
    makers = _names(info.get("maker"))
    series = _names(info.get("series"))

    ok, hits = is_allowed(title, genres, makers, actresses, series)
    if not ok:
        print(f"🛡️ 除外: {cid} ({hits[0]})")
        return None

    if extra_genre and extra_genre not in genres:
        genres = genres + [extra_genre]   # サイト側がこのジャンル名でカテゴリを判定する

    samples: list[str] = []
    block = item.get("sampleImageURL") or {}
    for key in ("sample_l", "sample_s"):
        imgs = (block.get(key) or {}).get("image") or []
        if isinstance(imgs, str):
            imgs = [imgs]
        if imgs:
            samples = [u for u in imgs if u][:12]
            break

    movie = ""
    movie_block = item.get("sampleMovieURL") or {}
    for key in ("size_720_480", "size_644_414", "size_560_360", "size_476_306"):
        if movie_block.get(key):
            movie = movie_block[key]
            break

    prices = item.get("prices") or {}
    review = item.get("review") or {}
    try:
        review_avg = float(review.get("average") or 0) or None
        review_count = int(review.get("count") or 0) or None
    except (TypeError, ValueError):
        review_avg, review_count = None, None

    return {
        "cid": cid,
        "title": title,
        "url": url,
        "image": image,
        "samples": samples,
        "movie": movie,
        "price": _yen(prices.get("price")),
        "list_price": _yen(prices.get("list_price")),
        "review_avg": review_avg,
        "review_count": review_count,
        "volume": _yen(item.get("volume")),
        "release_date": (item.get("date") or "")[:10] or None,
        "maker": makers[0] if makers else "",
        "series": series[0] if series else "",
        "genres": genres,
        "actresses": actresses,
    }


# ---------------------------------------------------------------- WP送信 ----

class WpClient:
    def __init__(self, cfg: Config) -> None:
        self.base = f"{cfg.wp_url}/wp-json/av/v1"
        self.s = requests.Session()
        self.s.auth = HTTPBasicAuth(cfg.wp_user, cfg.wp_password)
        self.s.headers["User-Agent"] = "av-sync/1.0"

    def post(self, path: str, payload: dict[str, Any], retries: int = 3) -> dict[str, Any]:
        last = ""
        for attempt in range(1, retries + 1):
            try:
                r = self.s.post(f"{self.base}/{path}", json=payload, timeout=120)
                if r.status_code == 200:
                    return r.json()
                last = f"HTTP {r.status_code}: {r.text[:200]}"
                if r.status_code in (401, 403):  # 認証系はリトライしても直らない
                    break
            except (requests.RequestException, ValueError) as e:
                last = str(e)
            time.sleep(3 * attempt)
        raise RuntimeError(f"{path} 送信失敗: {last}")


def wp_get(wp: WpClient, path: str) -> dict[str, Any] | None:
    """認証つきGET。失敗したら None（旧版のプラグインなどで窓口が無い場合を含む）。"""
    try:
        r = wp.s.get(f"{wp.base}/{path}", timeout=30)
        body = r.json() if r.status_code == 200 else None
        return body if isinstance(body, dict) else None
    except (requests.RequestException, ValueError):
        return None


def send_items(wp: WpClient, items: list[Item]) -> tuple[int, int]:
    ok = failed = 0
    for i in range(0, len(items), BATCH_SIZE):
        batch = items[i:i + BATCH_SIZE]
        try:
            res = wp.post("ingest", {"items": batch})
            ok += int(res.get("saved", 0))
            print(f"✅ 取り込み {i + len(batch)}/{len(items)} (保存{res.get('saved')}件)")
        except RuntimeError as e:
            failed += len(batch)
            print(f"❌ {e}", file=sys.stderr)
    return ok, failed


# ---------------------------------------------------------------- 診断 ----

def classify_auth(status: int, whoami: dict[str, Any] | None) -> str:
    """認証テストの結果から、失敗の原因を日本語で説明する。

    whoami は AVハブの /av/v1/whoami の応答。
      auth_header: 認証ヘッダーがサーバーのPHPまで届いたか
      user_id    : WordPress がそのヘッダーで認識したユーザー（0=認識できず）
      auth_error : WordPress が認証を拒否した理由コード
      app_pw_ok  : このリクエストでアプリケーションパスワードが使える状態か
      ssl        : WordPress がHTTPS接続と判定しているか
    """
    if status == 200:
        return "✅ 認証OK。WordPress がID/パスワードを受け取り、管理者として認識しています。"
    if whoami is None:
        return ("❌ 認証に失敗しました。原因を切り分ける診断窓口が見つかりません"
                "（mu-plugin が古い、または未設置）。最新の av_hub_mu_plugin.php を設置してください。")
    if not whoami.get("auth_header"):
        return ("❌ 原因: サーバーが認証ヘッダー（Authorization）を落としています。ID/パスワードは関係ありません。\n"
                "   対処: サイトの .htaccess の先頭に、次の4行を追加してください。\n"
                "     <IfModule mod_rewrite.c>\n"
                "     RewriteEngine On\n"
                "     RewriteRule .* - [E=HTTP_AUTHORIZATION:%{HTTP:Authorization}]\n"
                "     </IfModule>")
    err = whoami.get("auth_error") or ""
    if whoami.get("user_id"):
        return ("❌ ユーザーは認識できていますが、権限が足りません。"
                "WP_USERNAME を『管理者』権限のユーザーにしてください。")
    if err == "invalid_username":
        return ("❌ 原因: その WP_USERNAME のユーザーが、このサイトに存在しません。\n"
                "   対処: プロフィール画面の『ユーザー名』欄の文字を、そのまま WP_USERNAME に登録してください。\n"
                "   （サイトごとにユーザーは別です。19/20 のサイトのユーザー名ではありません）")
    if err == "incorrect_password":
        return ("❌ 原因: ユーザー名は正しいが、アプリケーションパスワードが違います。\n"
                "   対処: 『このサイト（adult-navi21.com）』のプロフィールでパスワードを作り直し、"
                "表示された24文字を WP_APP_PASSWORD に登録してください。")
    if err in ("application_passwords_disabled_for_user", "application_passwords_disabled"):
        return ("❌ 原因: このユーザー（またはサイト）でアプリケーションパスワードが無効にされています。\n"
                "   セキュリティ系のプラグインや設定が、無効化している可能性があります。")
    if whoami.get("app_pw_ok") is False:
        return ("❌ 原因: このリクエストではアプリケーションパスワードが使えない状態です。\n"
                f"   WordPress のHTTPS判定={whoami.get('ssl')}。HTTPSと判定されていない場合、"
                "サーバー設定（リバースプロキシ）の影響が考えられます。")
    return ("❌ 認証ヘッダーは届いていますが、WordPress が認証処理を行っていないようです"
            f"（理由コード={err or 'なし'} / HTTPS判定={whoami.get('ssl')}）。\n"
            "   他のプラグインやサーバー設定が、REST API の認証に介入している可能性があります。この行を共有してください。")


def check_wp(cfg: Config) -> int:
    """WordPress 側の接続・認証を、作品を送らずに診断する。"""
    base = cfg.wp_url
    pw_len = len(re.sub(r"\s", "", cfg.wp_password))
    print(f"🔎 WP_URL = {base} / ユーザー = {cfg.wp_user} / アプリパスワード = {pw_len}文字（通常24文字）")
    if pw_len != 24:
        print("⚠️ アプリケーションパスワードは通常24文字（スペース除く）です。コピー漏れ・別の文字列の可能性があります。")
    s = requests.Session()
    s.headers["User-Agent"] = "av-sync/1.0"

    def get(path: str, auth: bool = False) -> requests.Response | None:
        try:
            return s.get(f"{base}{path}", timeout=30,
                         auth=HTTPBasicAuth(cfg.wp_user, cfg.wp_password) if auth else None)
        except requests.RequestException as e:
            print(f"❌ 接続失敗 {path}: {e}")
            return None

    r = get("/wp-json/")
    if r is None:
        return 1
    print(f"1) REST API: HTTP {r.status_code} / 最終URL: {r.url}")
    if r.history:
        print(f"⚠️ リダイレクトされています（{r.history[0].url} → {r.url}）。"
              "WP_URL を最終URLのホスト（www有無・https）に合わせてください。転送時に認証情報が外れます。")
    try:
        has_app_pw = "application-passwords" in (r.json().get("authentication") or {})
    except ValueError:
        has_app_pw = False
    print("2) アプリケーションパスワード機能: " + ("有効" if has_app_pw else "❌ 無効（または REST が応答していません）"))

    r2 = get("/wp-json/av/v1")
    print("3) AVハブ（mu-plugin）の読み込み: " + ("✅ 読み込まれています" if r2 is not None and r2.status_code == 200
                                          else f"❌ 見つかりません（HTTP {r2.status_code if r2 is not None else '-'}）"))

    # 取り込みと同じ権限チェック（管理者のみ）を通るかを確認する。users/me はサーバー側で遮断されることがあるため使わない
    r3 = get("/wp-json/av/v1/ping", auth=True)
    if r3 is None:
        return 1
    try:
        body = r3.json()
    except ValueError:
        body = {}
    print(f"4) 認証テスト: HTTP {r3.status_code} / code={body.get('code', '-') if isinstance(body, dict) else '-'}")
    if r3.status_code == 403 and not isinstance(body, dict):
        print("   ⚠️ 応答がWordPressのものではありません。サーバー側で遮断されている可能性があります。")
    whoami: dict[str, Any] | None = None
    r4 = get("/wp-json/av/v1/whoami", auth=True)
    if r4 is not None and r4.status_code == 200:
        try:
            whoami = r4.json()
        except ValueError:
            whoami = None
        w = whoami or {}
        print(f"5) 診断窓口: 認証ヘッダー到達={w.get('auth_header')} / 認識されたユーザーID={w.get('user_id')} / "
              f"拒否理由={w.get('auth_error') or '-'} / アプリPW利用可={w.get('app_pw_ok')} / HTTPS判定={w.get('ssl')}")
    print(classify_auth(r3.status_code, whoami))
    return 0 if r3.status_code == 200 else 1


# ---------------------------------------------------------------- main ----

def dedupe(items: list[Item]) -> list[Item]:
    """同じ作品は1件にまとめる（後のデータを採用）。カテゴリ判定用に付けたジャンルは、どちらの分も残す。"""
    merged: dict[str, Item] = {}
    for it in items:
        old = merged.get(it["cid"])
        if old is not None:
            it = {**it, "genres": list(dict.fromkeys([*old.get("genres", []), *it.get("genres", [])]))}
        merged[it["cid"]] = it
    return list(merged.values())


def run(mode: str, dry_run: bool) -> int:
    cfg = load_config(need_wp=not dry_run)
    today = dt.datetime.now(JST)

    # 1) 人気順（ランキング）。順位は除外前の元の並びで記録する
    # ランキングとして保存するのは上位 rank_top_n 件だが、価格・レビューの更新（最安値の記録）のため、
    # 毎日は人気上位 refresh_n 件までを取り込み直す
    top_n = cfg.backfill_n if mode == "backfill" else max(cfg.rank_top_n, cfg.refresh_n)
    ranked_raw = fetch_items(cfg, "rank", top_n)
    # fetch_items は途中で API が失敗すると取得済み分だけを返す。欠けたランキングを保存すると
    # 翌日の「急上昇」「新規ランクイン」が実態とずれるため、期待件数の一定割合に満たなければ保存せず終了する
    min_ratio = float(os.environ.get("RANK_MIN_RATIO", "0.8"))
    if mode != "backfill" and len(ranked_raw) < int(top_n * min_ratio):
        print(f"❌ 人気順の取得が {len(ranked_raw)}/{top_n} 件でした（下限 {min_ratio:.0%}）。API障害の可能性があるため、保存せず終了します。", file=sys.stderr)
        return 1
    ranks: list[dict[str, Any]] = []
    normalized: list[Item] = []
    for pos, raw in enumerate(ranked_raw, start=1):
        n = normalize(raw, cfg)
        if n is None:
            continue
        normalized.append(n)
        if pos <= cfg.rank_top_n:
            ranks.append({"cid": n["cid"], "pos": pos})

    # 2) 新着（発売日の新しい順）
    days = 90 if mode == "backfill" else cfg.new_days
    gte = (today - dt.timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
    new_limit = cfg.backfill_n if mode == "backfill" else cfg.new_max
    for raw in fetch_items(cfg, "date", new_limit, gte_date=gte):
        n = normalize(raw, cfg)
        if n is not None:
            normalized.append(n)

    # 3) 特化カテゴリ（NTR / ハード系 / 素人 など）: 全体の人気上位に入らない作品も集め、カテゴリ内のランキングを別に記録する
    cat_ranks: dict[str, list[dict[str, Any]]] = {}
    degraded = False   # 一部のカテゴリが集められなかった（終了コードを1にして気づけるようにする）
    for cat in cfg.categories:
        res = collect_category(cfg, cat, mode, gte, min_ratio)
        if res is None:
            degraded = True
            continue
        cat_items, cat_rank, complete = res
        normalized.extend(cat_items)
        if complete or mode == "backfill":
            cat_ranks[cat.slug] = cat_rank
        else:
            print(f"⚠️ カテゴリ「{cat.slug}」: 人気順の取得が不足しているため、順位は保存しません（作品の取り込みは行います）。", file=sys.stderr)
            degraded = True
        print(f"🎯 {cat.label}（{cat.slug}）: 取り込み {len(cat_items)}件 / ランキング {len(cat_rank)}件")

    items = dedupe(normalized)
    print(f"📦 取得 {len(ranked_raw)}件(人気) → 取り込み対象 {len(items)}件 / ランキング {len(ranks)}件")
    if not items:
        print("❌ 取り込み対象が0件です（APIキーやAPI障害を確認してください）。", file=sys.stderr)
        return 1

    if dry_run:
        for it in items[:3]:
            print({k: (v if k != "samples" else f"[{len(v)}枚]") for k, v in it.items()})
        return 0

    wp = WpClient(cfg)
    _, failed = send_items(wp, items)

    # ランキングは作品の取り込み後に送る（WP側で作品IDに紐づけるため）
    try:
        res = wp.post("rank", {"day": today.strftime("%Y-%m-%d"), "ranks": ranks})
        print(f"✅ ランキング保存 {res.get('saved')}件 ({res.get('day')})")
    except RuntimeError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    # カテゴリごとのランキング。旧版のプラグインに送ると全体のランキングを上書きしてしまうため、
    # サイト側がそのカテゴリに対応していることを確かめてから送る
    info = wp_get(wp, "ping") or {}
    site_cats = set(info.get("cats") or [])
    for cat in cfg.categories:
        ranks_c = cat_ranks.get(cat.slug)
        if not ranks_c:
            continue
        if not cat.primary and cat.slug not in site_cats:
            print(f"⚠️ サイト側にカテゴリ「{cat.slug}」がありません（プラグインが v1.6.0 より古い、または AV_HUB_CATS に未登録）。"
                  "順位は保存せずスキップします。", file=sys.stderr)
            degraded = True
            continue
        payload: dict[str, Any] = {"day": today.strftime("%Y-%m-%d"), "ranks": ranks_c, "cat": cat.slug}
        if cat.primary:
            payload["focus"] = True   # 旧版のプラグインでも、従来の特化ランキングに保存される
        try:
            res = wp.post("rank", payload)
            print(f"✅ {cat.label}ランキング保存 {res.get('saved')}件 ({res.get('day')})")
        except RuntimeError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1
    return 1 if (failed or degraded) else 0


def check_categories(cfg: Config) -> int:
    """content/categories.json の設定を DMM API で確認する（WordPress には送らない）。"""
    bad = 0
    print(f"📋 カテゴリ {len(cfg.categories)}件（content/categories.json）")
    for cat in cfg.categories:
        print(f"\n■ {cat.label}（/av/{cat.slug}/）floor={cat.floor}" + ("  [primary]" if cat.primary else ""))
        article_id: str | None = None
        if cat.genre_name:
            article_id, cands = resolve_genre_id(cfg, cat.floor, cat.genre_name)
            if not article_id:
                print(f"  ❌ ジャンル「{cat.genre_name}」のIDを特定できません。候補: {' / '.join(cands) or 'なし'}")
                bad += 1
                continue
            print(f"  ジャンル「{cat.genre_name}」→ ID {article_id}（一致: {' / '.join(cands)}）")
        raw = fetch_items(cfg, "rank", 5, keyword=cat.keywords[0] if cat.keywords else None, floor=cat.floor, article_id=article_id)
        if not raw:
            print("  ❌ 作品を取得できませんでした（floor・キーワード・ジャンルの指定を確認してください）。")
            bad += 1
            continue
        kept = [normalize(r, cfg, extra_genre=cat.add_genre) for r in raw]
        print(f"  人気上位 {len(raw)}件を取得（取り込み対象 {sum(1 for k in kept if k)}件）")
        for k in [k for k in kept if k][:3]:
            print(f"   - {k['title'][:30]}… / ジャンル: {', '.join(k['genres'][:5])}")
    print("\n" + ("✅ すべて取得できました。" if not bad else f"❌ {bad}件のカテゴリに問題があります。"))
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="FANZA→WordPress AVハブ同期")
    ap.add_argument("--mode", choices=("daily", "backfill", "check", "cats"), default="daily",
                    help="check=WordPressとの接続・認証の診断だけを行う / cats=特化カテゴリの設定をDMM APIで確認する")
    ap.add_argument("--dry-run", action="store_true", help="WPへ送信せず、取得結果だけ確認する")
    args = ap.parse_args()
    if args.mode == "check":
        return check_wp(load_config(need_wp=True, need_dmm=False))
    if args.mode == "cats":
        return check_categories(load_config(need_wp=False))
    return run(args.mode, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
