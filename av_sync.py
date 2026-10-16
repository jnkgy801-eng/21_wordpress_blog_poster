# -*- coding: utf-8 -*-
"""
FANZA動画（AV）→ WordPress（AVハブ）同期スクリプト。GitHub Actions から実行する。

  python av_sync.py --mode daily      # 毎日: 人気上位 + 直近の新着を取り込み、当日のランキングを保存
  python av_sync.py --mode backfill   # 初回: 人気順で多めに取り込む（カタログの土台づくり）
  python av_sync.py --mode daily --dry-run   # 取得とフィルターだけ確認（WPには送らない）

必須の環境変数:
  DMM_API_ID / DMM_AFFILIATE_ID
  WP_URL / WP_USERNAME / WP_APP_PASSWORD   （WP側はアプリケーションパスワード。管理者権限のユーザー）
任意:
  RANK_TOP_N(既定100) NEW_DAYS(既定14) NEW_MAX(既定300) BACKFILL_N(既定2000) EXTRA_EXCLUDE
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

JST = dt.timezone(dt.timedelta(hours=9))
DMM_API = "https://api.dmm.com/affiliate/v3/ItemList"
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


def load_config(need_wp: bool, need_dmm: bool = True) -> Config:
    # Secrets の貼り付け時に混ざりやすい前後の空白・改行は除去する
    env = lambda k, d="": os.environ.get(k, d).strip()  # noqa: E731
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
    )


# ---------------------------------------------------------------- 取得 ----

def fetch_items(cfg: Config, sort: str, limit: int, gte_date: str | None = None) -> list[Item]:
    """FANZA動画をページングで取得する。途中で失敗した場合は取得済み分を返す。"""
    out: list[Item] = []
    offset = 1
    while len(out) < limit:
        params = {
            "api_id": cfg.dmm_api_id,
            "affiliate_id": cfg.dmm_affiliate_id,
            "site": "FANZA",
            "service": "digital",
            "floor": "videoa",
            "hits": min(PAGE_SIZE, limit - len(out)),
            "offset": offset,
            "sort": sort,
            "output": "json",
            # 未来日の予約作品は、購入できず記事価値も低いので最初から除外する
            "lte_date": dt.datetime.now(JST).strftime("%Y-%m-%dT%H:%M:%S"),
        }
        if gte_date:
            params["gte_date"] = gte_date
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


def normalize(item: Item, cfg: Config) -> Item | None:
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
    if not whoami.get("user_id"):
        return ("❌ 原因: 認証ヘッダーは届いていますが、WordPress がユーザーとして認識できません。\n"
                "   確認: ①WP_USERNAME はログイン用のユーザー名か（表示名・メールアドレスではない）\n"
                "        ②WP_APP_PASSWORD は『アプリケーションパスワード』か（通常のログインパスワードではない）\n"
                "        ③そのユーザーのプロフィールで、アプリケーションパスワードを作り直して再登録する")
    return ("❌ ユーザーは認識できていますが、権限が足りません。"
            "WP_USERNAME を『管理者』権限のユーザーにしてください。")


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

    r3 = get("/wp-json/wp/v2/users/me", auth=True)
    if r3 is None:
        return 1
    print(f"4) 認証テスト: HTTP {r3.status_code}")
    whoami: dict[str, Any] | None = None
    r4 = get("/wp-json/av/v1/whoami", auth=True)
    if r4 is not None and r4.status_code == 200:
        try:
            whoami = r4.json()
        except ValueError:
            whoami = None
        print(f"5) 診断窓口: 認証ヘッダー到達={whoami.get('auth_header') if whoami else '-'} / 認識されたユーザーID={whoami.get('user_id') if whoami else '-'}")
    print(classify_auth(r3.status_code, whoami))
    return 0 if r3.status_code == 200 else 1


# ---------------------------------------------------------------- main ----

def dedupe(items: list[Item]) -> list[Item]:
    return list({it["cid"]: it for it in items}.values())


def run(mode: str, dry_run: bool) -> int:
    cfg = load_config(need_wp=not dry_run)
    today = dt.datetime.now(JST)

    # 1) 人気順（ランキング）。順位は除外前の元の並びで記録する
    top_n = cfg.backfill_n if mode == "backfill" else max(cfg.rank_top_n, 100)
    ranked_raw = fetch_items(cfg, "rank", top_n)
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
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="FANZA→WordPress AVハブ同期")
    ap.add_argument("--mode", choices=("daily", "backfill", "check"), default="daily",
                    help="check=WordPressとの接続・認証の診断だけを行う")
    ap.add_argument("--dry-run", action="store_true", help="WPへ送信せず、取得結果だけ確認する")
    args = ap.parse_args()
    if args.mode == "check":
        return check_wp(load_config(need_wp=True, need_dmm=False))
    return run(args.mode, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
