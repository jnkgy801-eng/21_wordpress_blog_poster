#!/usr/bin/env python3
"""
週次まとめ画像の自動作成（無料）。

サイト（av_hub プラグイン）の公開データ /wp-json/av/v1/weekly を読み、
  ① 今週の急上昇TOP5（順位を上げた作品・新規ランクイン作品）
  ② 今週の値下がり（記録された値下がり額が大きい順）
の画像（PNG）と、投稿用の文章を out/ に作ります。表紙画像は使わず、文字だけのカードです
（画像の権利や、SNSの審査に引っかかる心配を減らすため）。

必須の環境変数:
  WP_URL            サイトのURL（例 https://adult-navi21.com）
任意:
  OUT_DIR           出力先（既定 out）
  FOCUS             1=特化ジャンル（NTR）/ 0=全体（既定 1）
  HASHTAGS          投稿文の末尾に付けるタグ（既定 "#NTR #寝取られ"）
  MASK_WORDS        画像内で伏せたい語（カンマ区切り。例 "語1,語2"）→ ○○ に置き換え
  ACCENT            カードの強調色（既定 #6366f1）
  --- 次の2つが両方あるときだけ、画像をサイトのメディアにアップロードします ---
  WP_USERNAME / WP_APP_PASSWORD
  --- Buffer に予約投稿する場合（任意。Buffer の仕様変更で動かない場合はスキップされます）---
  BUFFER_ACCESS_TOKEN / BUFFER_PROFILE_IDS（カンマ区切り）

使い方:
  python av_weekly_card.py            # 画像と文章を out/ に作る
  python av_weekly_card.py --post     # さらに、アップロード・Buffer投稿まで試す
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 675
BG = (17, 19, 24)
CARD = (28, 31, 39)
TEXT = (236, 238, 243)
MUTED = (150, 156, 170)
GREEN = (74, 222, 128)

FONT_PATHS = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    "C:/Windows/Fonts/meiryo.ttc",
]
BOLD_PATHS = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
]


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for path in (BOLD_PATHS if bold else []) + FONT_PATHS:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size, index=0)
            except OSError:
                continue
    sys.exit("❌ 日本語フォントが見つかりません。Actions では `sudo apt-get install -y fonts-noto-cjk` を先に実行してください。")


def hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return (99, 102, 241)


def fit(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> str:
    """幅に収まるように末尾を … で切る"""
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def mask(text: str, words: list[str]) -> str:
    for w in words:
        if w:
            text = text.replace(w, "○" * len(w))
    return text


def yen(n: int | None) -> str:
    return "—" if n is None else f"¥{n:,}"


def span_of(data: dict[str, Any]) -> int:
    """集計の日数。プラグインが古く span を返さない場合は 7 日（週間）として扱う。"""
    try:
        return max(1, int(data.get("span") or 7))
    except (TypeError, ValueError):
        return 7


def period_label(data: dict[str, Any]) -> str:
    return "今日" if span_of(data) == 1 else "今週"


def span_phrase(data: dict[str, Any]) -> str:
    span = span_of(data)
    return "前日から" if span == 1 else f"直近{span}日で"


def fetch(site: str, focus: bool, days: int = 7) -> dict[str, Any]:
    params = {"focus": "1" if focus else "0", "days": str(days)}
    last: Exception | None = None
    for url, p in ((f"{site}/wp-json/av/v1/weekly", params), (f"{site}/", {**params, "rest_route": "/av/v1/weekly"})):
        try:
            r = requests.get(url, params=p, timeout=30, headers={"User-Agent": "av-weekly-card/1.0"})
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"):
                return r.json()
            last = RuntimeError(f"HTTP {r.status_code}")
        except requests.RequestException as e:
            last = e
    sys.exit(f"❌ 週次データを取得できません（{last}）。サイトのプラグインが v1.5.0 以上か確認してください（days 指定は v1.5.0 から）。")


def base_canvas(title: str, subtitle: str, site: str, accent: tuple[int, int, int]) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 10, H], fill=accent)
    d.text((48, 30), site, font=load_font(22), fill=MUTED)
    d.text((48, 62), title, font=load_font(46, True), fill=TEXT)
    d.text((48, 126), subtitle, font=load_font(22), fill=MUTED)
    return img, d


def footer(d: ImageDraw.ImageDraw, url: str) -> None:
    d.text((48, H - 44), url, font=load_font(22), fill=MUTED)


def rank_card(data: dict[str, Any], rows: list[dict[str, Any]], mode: str, words: list[str], accent: tuple[int, int, int]) -> Image.Image:
    label = data.get("label") or ""
    if mode == "movers":
        title = f"{period_label(data)}の{label}急上昇 TOP{len(rows)}"
        sub = f"{(data.get('base_day') or '')[5:].replace('-', '/')} → {(data.get('day') or '')[5:].replace('-', '/')} の人気順位の変化"
    else:
        title = f"いまの{label}人気 TOP{len(rows)}"
        sub = f"{(data.get('day') or '')[5:].replace('-', '/')} 時点の人気順位"
    img, d = base_canvas(title, sub, data.get("site", ""), accent)
    f_t, f_s, f_n, f_b = load_font(30, True), load_font(21), load_font(34, True), load_font(26, True)
    top, step = 176, 90
    for i, r in enumerate(rows):
        y = top + i * step
        d.rounded_rectangle([40, y, W - 40, y + step - 10], radius=12, fill=CARD)
        d.ellipse([58, y + 12, 108, y + 62], fill=accent)
        num = str(i + 1)
        d.text((83 - d.textlength(num, font=f_n) / 2, y + 17), num, font=f_n, fill=(255, 255, 255))
        d.text((128, y + 8), fit(d, mask(r["title"], words), f_t, 700), font=f_t, fill=TEXT)
        score = f"★{r['review_avg']:.1f}（{r['review_cnt']}件）" if r.get("review_avg") and r.get("review_cnt") else "レビュー少なめ"
        lp = f"・定価{yen(r['list_price'])}" if r.get("list_price") and r.get("price") and r["list_price"] > r["price"] else ""
        d.text((128, y + 48), f"{score}{lp}", font=f_s, fill=MUTED)
        # 右側: 順位の変化と価格
        if mode == "movers":
            if r.get("pos_then") is None:
                badge = "NEW IN"
            else:
                badge = f"↑{r['pos_then'] - r['pos_now']}位"
            sub_r = f"{r['pos_now']}位"
        else:
            badge, sub_r = f"{r['pos_now']}位", ""
        bw = d.textlength(badge, font=f_b)
        d.text((W - 80 - 190 - bw, y + 10), badge, font=f_b, fill=GREEN)
        if sub_r:
            d.text((W - 80 - 190 - d.textlength(sub_r, font=f_s), y + 48), sub_r, font=f_s, fill=MUTED)
        price = yen(r.get("price"))
        d.text((W - 70 - d.textlength(price, font=f_b), y + 24), price, font=f_b, fill=TEXT)
    footer(d, data.get("list_url", data.get("home", "")))
    return img


def drop_card(data: dict[str, Any], rows: list[dict[str, Any]], words: list[str], accent: tuple[int, int, int]) -> Image.Image:
    label = data.get("label") or ""
    img, d = base_canvas(f"{period_label(data)}の{label}値下がり TOP{len(rows)}", f"{span_phrase(data)}、価格が下がった作品（当サイトの価格記録より）", data.get("site", ""), accent)
    f_t, f_s, f_n, f_b = load_font(30, True), load_font(21), load_font(34, True), load_font(26, True)
    top, step = 176, 90
    for i, r in enumerate(rows):
        y = top + i * step
        d.rounded_rectangle([40, y, W - 40, y + step - 10], radius=12, fill=CARD)
        d.ellipse([58, y + 12, 108, y + 62], fill=accent)
        num = str(i + 1)
        d.text((83 - d.textlength(num, font=f_n) / 2, y + 17), num, font=f_n, fill=(255, 255, 255))
        d.text((128, y + 8), fit(d, mask(r["title"], words), f_t, 700), font=f_t, fill=TEXT)
        score = f"★{r['review_avg']:.1f}（{r['review_cnt']}件）" if r.get("review_avg") and r.get("review_cnt") else "レビュー少なめ"
        d.text((128, y + 48), score, font=f_s, fill=MUTED)
        change = f"{yen(r['from_price'])} → {yen(r['price'])}"
        d.text((W - 70 - d.textlength(change, font=f_b), y + 10), change, font=f_b, fill=TEXT)
        saved = f"{yen(r['saved'])} おトク"
        d.text((W - 70 - d.textlength(saved, font=f_s), y + 48), saved, font=f_s, fill=GREEN)
    footer(d, data.get("log_url", data.get("home", "")))
    return img


def make_post_text(data: dict[str, Any], has_movers: bool, has_drops: bool, hashtags: str) -> str:
    label = data.get("label") or ""
    head = f"【{period_label(data)}の{label}急上昇TOP5】" if has_movers else f"【いまの{label}人気TOP5】"
    lines = [head]
    if has_drops:
        lines.append("値下がりした作品もまとめました")
    lines.append("順位の変化と価格の記録はこちら↓")
    lines.append(data.get("list_url", data.get("home", "")))
    if hashtags:
        lines.append(hashtags)
    return "\n".join(lines)


def upload_media(site: str, user: str, pw: str, path: Path) -> str | None:
    try:
        r = requests.post(
            f"{site}/wp-json/wp/v2/media",
            data=path.read_bytes(),
            headers={"Content-Disposition": f'attachment; filename="{path.name}"', "Content-Type": "image/png"},
            auth=(user, pw),
            timeout=60,
        )
        if r.status_code in (200, 201):
            return r.json().get("source_url")
        print(f"⚠️ 画像のアップロードに失敗しました（HTTP {r.status_code}）。")
    except requests.RequestException as e:
        print(f"⚠️ 画像のアップロードに失敗しました（{e}）。")
    return None


def post_buffer(token: str, profile_ids: list[str], text: str, media_url: str | None) -> None:
    """Buffer の旧API（updates/create）で予約投稿を試す。仕様変更で失敗しても処理は止めない。"""
    for pid in profile_ids:
        payload: dict[str, Any] = {"access_token": token, "profile_ids[]": pid, "text": text}
        if media_url:
            payload["media[photo]"] = media_url
        try:
            r = requests.post("https://api.bufferapp.com/1/updates/create.json", data=payload, timeout=30)
            ok = r.status_code == 200 and r.json().get("success")
            print(("✅ Buffer に追加: " if ok else f"⚠️ Buffer への追加に失敗（HTTP {r.status_code}）: ") + pid)
        except (requests.RequestException, ValueError) as e:
            print(f"⚠️ Buffer への追加に失敗: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--post", action="store_true", help="画像のアップロードと Buffer 投稿まで試す")
    args = ap.parse_args()

    env = lambda k, d="": os.environ.get(k, d).strip()  # noqa: E731
    site = env("WP_URL").rstrip("/")
    if not site:
        sys.exit("❌ WP_URL が未設定です。")
    out = Path(env("OUT_DIR", "out"))
    out.mkdir(parents=True, exist_ok=True)
    focus = env("FOCUS", "1") != "0"
    words = [w.strip() for w in env("MASK_WORDS").split(",") if w.strip()]
    accent = hex_to_rgb(env("ACCENT", "#6366f1"))
    hashtags = env("HASHTAGS", "#NTR #寝取られ")

    data = fetch(site, focus)
    (out / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    stamp = datetime.now().strftime("%Y%m%d")

    files: list[Path] = []
    movers = data.get("movers") or []
    top = data.get("top") or []
    drops = data.get("drops") or []
    has_movers = len(movers) >= 3
    if has_movers:
        p = out / f"weekly_movers_{stamp}.png"
        rank_card(data, movers, "movers", words, accent).save(p)
        files.append(p)
    elif len(top) >= 3:
        p = out / f"weekly_top_{stamp}.png"
        rank_card(data, top, "top", words, accent).save(p)
        files.append(p)
    has_drops = len(drops) >= 3
    if has_drops:
        p = out / f"weekly_drops_{stamp}.png"
        drop_card(data, drops, words, accent).save(p)
        files.append(p)

    if not files:
        print("ℹ️ まだ画像にできるデータが足りません（ランキング・価格の記録がたまるまで待ってください）。")
        return 0

    text = make_post_text(data, has_movers, has_drops, hashtags)
    (out / "post.txt").write_text(text, encoding="utf-8")
    for f in files:
        print(f"🖼  {f}")
    print("📝 投稿文:\n" + text)

    if args.post:
        user, pw = env("WP_USERNAME"), env("WP_APP_PASSWORD")
        media_url = upload_media(site, user, pw, files[0]) if user and pw else None
        if media_url:
            print(f"✅ 画像をアップロード: {media_url}")
        token, pids = env("BUFFER_ACCESS_TOKEN"), [x.strip() for x in env("BUFFER_PROFILE_IDS").split(",") if x.strip()]
        if token and pids:
            post_buffer(token, pids, text, media_url)
        else:
            print("ℹ️ BUFFER_ACCESS_TOKEN / BUFFER_PROFILE_IDS が未設定のため、投稿はスキップしました（画像と文章は out/ にあります）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
