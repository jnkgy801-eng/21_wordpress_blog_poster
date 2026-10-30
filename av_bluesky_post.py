#!/usr/bin/env python3
"""AVハブ → Bluesky 自動投稿（無料）。

サイトの REST（/wp-json/av/v1/weekly）から「急上昇」「値下がり」を取得し、
av_weekly_card.py と同じ画像カードを作って Bluesky に投稿します。
Bluesky の API は無料で、アプリパスワードだけで投稿できます（追加ライブラリ不要）。

環境変数
  WP_URL              サイトのURL（必須）
  BSKY_HANDLE         例 yourname.bsky.social（投稿時は必須）
  BSKY_APP_PASSWORD   Bluesky の「アプリパスワード」（投稿時は必須。通常のパスワードは使わない）
  BSKY_SERVICE        既定 https://bsky.social
  BSKY_LABEL          投稿に付ける自己申告ラベル。porn / sexual / nudity / graphic-media（既定 porn）
  BSKY_DAYS           1=前日比（日次）/ 7=週間（既定 1）
  FOCUS, MASK_WORDS, ACCENT, HASHTAGS, OUT_DIR は av_weekly_card.py と同じ

使い方
  python av_bluesky_post.py --dry-run   # 画像と文章を out/ に作るだけ（投稿しない）
  python av_bluesky_post.py             # 投稿する
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

import requests
from PIL import Image

import av_weekly_card as card
from social_filter import filter_rows

MAX_GRAPHEMES = 300          # Bluesky の投稿文の上限
MAX_BLOB_BYTES = 950_000     # 画像1枚の上限（公式は約1MB。余裕を見る）
MAX_ALT_CHARS = 900
VALID_LABELS = {"porn", "sexual", "nudity", "graphic-media"}
HTTP_TIMEOUT = 30
RETRY_STATUS = {429, 500, 502, 503, 504}


class Session(TypedDict):
    did: str
    accessJwt: str


class BlueskyError(RuntimeError):
    """Bluesky API の呼び出しに失敗した。"""


def _request(method: str, url: str, *, token: str | None = None, retries: int = 3, **kwargs: Any) -> dict[str, Any]:
    """JSON を返す API 呼び出し。429/5xx は待って再試行し、それ以外の失敗は BlueskyError にする。"""
    headers = dict(kwargs.pop("headers", {}))
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last = ""
    for attempt in range(1, retries + 1):
        try:
            r = requests.request(method, url, headers=headers, timeout=HTTP_TIMEOUT, **kwargs)
        except requests.RequestException as e:
            last = f"通信エラー: {e}"
        else:
            if r.status_code < 300:
                try:
                    return r.json()
                except ValueError as e:
                    raise BlueskyError(f"JSON として読めない応答です: {e}") from e
            last = f"HTTP {r.status_code}: {r.text[:300]}"
            if r.status_code not in RETRY_STATUS:
                break
        if attempt < retries:
            time.sleep(2 * attempt)
    raise BlueskyError(f"{method} {url} に失敗しました（{last}）")


def create_session(service: str, handle: str, app_password: str) -> Session:
    data = _request(
        "POST", f"{service}/xrpc/com.atproto.server.createSession",
        json={"identifier": handle, "password": app_password}, retries=1,
    )
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
    raise BlueskyError("画像が大きすぎて投稿できません。")


def upload_blob(service: str, session: Session, data: bytes, mime: str) -> dict[str, Any]:
    res = _request(
        "POST", f"{service}/xrpc/com.atproto.repo.uploadBlob",
        token=session["accessJwt"], headers={"Content-Type": mime}, data=data,
    )
    return res["blob"]


_URL_RE = re.compile(r"https?://[^\s]+")
_TAG_RE = re.compile(r"(?<!\S)#[^\s#。、，．,.!?！？]+")


def build_facets(text: str) -> list[dict[str, Any]]:
    """URL とハッシュタグをリンクとして認識させる（位置は UTF-8 のバイト単位で指定する）。"""
    def span(m: re.Match[str]) -> dict[str, int]:
        return {
            "byteStart": len(text[: m.start()].encode("utf-8")),
            "byteEnd": len(text[: m.end()].encode("utf-8")),
        }

    facets: list[dict[str, Any]] = []
    for m in _URL_RE.finditer(text):
        facets.append({"index": span(m), "features": [{"$type": "app.bsky.richtext.facet#link", "uri": m.group()}]})
    for m in _TAG_RE.finditer(text):
        facets.append({"index": span(m), "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": m.group()[1:]}]})
    return facets


def fit_text(text: str) -> str:
    """300字を超える場合は、末尾（ハッシュタグ行）から削って収める。"""
    lines = text.split("\n")
    while len("\n".join(lines)) > MAX_GRAPHEMES and len(lines) > 1:
        lines.pop()
    return "\n".join(lines)[:MAX_GRAPHEMES]


def create_post(service: str, session: Session, text: str, images: list[tuple[dict[str, Any], str]], label: str) -> dict[str, Any]:
    record: dict[str, Any] = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "langs": ["ja"],
        "facets": build_facets(text),
        # 成人向けの投稿であることを自己申告する（未設定だとアカウント制限の対象になりうる）
        "labels": {"$type": "com.atproto.label.defs#selfLabels", "values": [{"val": label}]},
    }
    if images:
        record["embed"] = {
            "$type": "app.bsky.embed.images",
            "images": [
                {"alt": alt, "image": blob, "aspectRatio": {"width": card.W, "height": card.H}}
                for blob, alt in images
            ],
        }
    return _request(
        "POST", f"{service}/xrpc/com.atproto.repo.createRecord", token=session["accessJwt"],
        json={"repo": session["did"], "collection": "app.bsky.feed.post", "record": record},
    )


def alt_text(heading: str, rows: list[dict[str, Any]], words: list[str]) -> str:
    """画像の代替テキスト（読み上げ・画像が見えない環境向け）。"""
    parts = [f"{i}. {card.mask(str(r['title']), words)}" for i, r in enumerate(rows, start=1)]
    return (heading + "。" + " / ".join(parts))[:MAX_ALT_CHARS]


def build_cards(data: dict[str, Any], words: list[str], accent: tuple[int, int, int], out: Path) -> tuple[list[tuple[Path, str]], bool, bool, int]:
    """投稿用の画像カードを作る。戻り値: ([(画像パス, 代替テキスト)], 急上昇あり, 値下がりあり, 1枚目の掲載件数)。"""
    stamp = datetime.now().strftime("%Y%m%d")
    # 外部サービスに載せられないタイトルは、画像に入れる前に除く
    movers = filter_rows(data.get("movers") or [])
    top = filter_rows(data.get("top") or [])
    drops = filter_rows(data.get("drops") or [])
    period, label = card.period_label(data), data.get("label") or ""

    cards: list[tuple[Path, str]] = []
    n_main = 0
    has_movers = len(movers) >= 3
    if has_movers:
        n_main = len(movers)
        p = out / f"bsky_movers_{stamp}.png"
        card.rank_card(data, movers, "movers", words, accent).save(p)
        cards.append((p, alt_text(f"{period}の{label}急上昇ランキング", movers, words)))
    elif len(top) >= 3:
        n_main = len(top)
        p = out / f"bsky_top_{stamp}.png"
        card.rank_card(data, top, "top", words, accent).save(p)
        cards.append((p, alt_text(f"いまの{label}人気ランキング", top, words)))
    has_drops = len(drops) >= 3
    if has_drops:
        p = out / f"bsky_drops_{stamp}.png"
        card.drop_card(data, drops, words, accent).save(p)
        cards.append((p, alt_text(f"{period}の{label}値下がりランキング", drops, words)))
    return cards, has_movers, has_drops, n_main


def main() -> int:
    ap = argparse.ArgumentParser(description="AVハブ → Bluesky 自動投稿")
    ap.add_argument("--dry-run", action="store_true", help="画像と文章を作るだけで、投稿しない")
    args = ap.parse_args()

    # GitHub Actions は未設定の vars を「空文字」で渡す。空のときも既定値 d を使う
    env = lambda k, d="": os.environ.get(k, "").strip() or d  # noqa: E731
    site = env("WP_URL").rstrip("/")
    if not site:
        sys.exit("❌ WP_URL が未設定です。")
    label = env("BSKY_LABEL", "porn")
    if label not in VALID_LABELS:
        sys.exit(f"❌ BSKY_LABEL は {sorted(VALID_LABELS)} のどれかにしてください（現在: {label}）。")
    try:
        days = int(env("BSKY_DAYS", "1"))
    except ValueError:
        sys.exit("❌ BSKY_DAYS は整数（1 または 7）で指定してください。")

    out = Path(env("OUT_DIR", "out"))
    out.mkdir(parents=True, exist_ok=True)
    words = [w.strip() for w in env("MASK_WORDS").split(",") if w.strip()]
    accent = card.hex_to_rgb(env("ACCENT", "#6366f1"))
    hashtags = env("HASHTAGS", "#NTR #寝取られ")

    data = card.fetch(site, env("FOCUS", "1") != "0", days)
    cards, has_movers, has_drops, n_main = build_cards(data, words, accent, out)
    if not cards:
        print("ℹ️ 投稿できるデータが足りないため、今回は投稿しません（ランキング・価格の記録がたまるまで待つか、除外された作品が多い可能性があります）。")
        return 0

    # make_post_text は「TOP5」固定のため、除外で件数が減ったときはカードの件数に合わせる
    text = fit_text(card.make_post_text(data, has_movers, has_drops, hashtags).replace("TOP5", f"TOP{n_main}"))
    (out / "bsky_post.txt").write_text(text, encoding="utf-8")
    (out / "bsky_alt.json").write_text(json.dumps([a for _, a in cards], ensure_ascii=False, indent=2), encoding="utf-8")
    print("📝 投稿文:\n" + text)
    for p, _ in cards:
        print(f"🖼  {p}")
    if args.dry_run:
        print("ℹ️ --dry-run のため投稿していません。")
        return 0

    handle, password = env("BSKY_HANDLE"), env("BSKY_APP_PASSWORD")
    if not handle or not password:
        sys.exit("❌ BSKY_HANDLE / BSKY_APP_PASSWORD が未設定です。")
    service = env("BSKY_SERVICE", "https://bsky.social").rstrip("/")
    try:
        session = create_session(service, handle, password)
        uploaded: list[tuple[dict[str, Any], str]] = []
        for p, alt in cards:
            data_bytes, mime = encode_image(Image.open(p))
            uploaded.append((upload_blob(service, session, data_bytes, mime), alt))
        res = create_post(service, session, text, uploaded, label)
    except (BlueskyError, KeyError) as e:
        sys.exit(f"❌ Bluesky への投稿に失敗しました: {e}")
    print(f"✅ 投稿しました: {res.get('uri', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
