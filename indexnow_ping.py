#!/usr/bin/env python3
"""IndexNow で、更新されたページを Bing / Yandex などに通知する（無料・任意）。

サイトマップ（/av-sitemap.xml）のうち、lastmod が「昨日以降」のURLだけを送ります。
Google は IndexNow に対応していないため、Google への通知は Search Console のサイトマップ送信で行います。

事前準備
  1. 英数字とハイフンの8〜128文字のキーを決める（例: openssl rand -hex 16）
  2. プラグインの定数 AV_HUB_INDEXNOW_KEY に同じ値を設定する（/{キー}.txt が自動で公開される）
  3. GitHub の Variables に INDEXNOW_KEY として同じ値を登録する

環境変数
  WP_URL         サイトのURL（必須）
  INDEXNOW_KEY   上記のキー（未設定なら何もせず終了）
"""
from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

ENDPOINT = "https://api.indexnow.org/indexnow"
SITEMAP_PATH = "/av-sitemap.xml"
NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
JST = timezone(timedelta(hours=9))
BATCH = 10_000   # IndexNow の1回あたりの上限
TIMEOUT = 30


def parse_sitemap(xml_text: str) -> list[tuple[str, date | None]]:
    """サイトマップから (URL, lastmod) の一覧を取り出す。lastmod が無い／読めないものは None。"""
    root = ET.fromstring(xml_text)
    out: list[tuple[str, date | None]] = []
    for u in root.findall("sm:url", NS):
        loc = (u.findtext("sm:loc", default="", namespaces=NS) or "").strip()
        raw = (u.findtext("sm:lastmod", default="", namespaces=NS) or "").strip()
        if not loc:
            continue
        try:
            lastmod: date | None = date.fromisoformat(raw[:10]) if raw else None
        except ValueError:
            lastmod = None
        out.append((loc, lastmod))
    return out


def pick_recent(entries: list[tuple[str, date | None]], since: date) -> list[str]:
    """lastmod が since 以降のURLだけを、重複なしで返す（lastmod の無いURLは対象外）。"""
    return list(dict.fromkeys(url for url, lm in entries if lm is not None and lm >= since))


def submit(host: str, key: str, key_location: str, urls: list[str]) -> bool:
    ok = True
    for i in range(0, len(urls), BATCH):
        chunk = urls[i : i + BATCH]
        try:
            r = requests.post(
                ENDPOINT,
                json={"host": host, "key": key, "keyLocation": key_location, "urlList": chunk},
                headers={"Content-Type": "application/json; charset=utf-8"},
                timeout=TIMEOUT,
            )
        except requests.RequestException as e:
            print(f"❌ IndexNow への送信に失敗しました: {e}", file=sys.stderr)
            ok = False
            continue
        if r.status_code in (200, 202):
            print(f"✅ IndexNow に {len(chunk)} 件を通知しました（HTTP {r.status_code}）")
        else:
            hint = {
                403: "キーが認識されません。/{キー}.txt がサイトで開けるか確認してください。",
                422: "URLがホストと一致しない、またはキーの形式が不正です。",
                429: "送信が多すぎます。時間をおいてください。",
            }.get(r.status_code, "")
            print(f"❌ IndexNow がエラーを返しました（HTTP {r.status_code}）{hint}", file=sys.stderr)
            ok = False
    return ok


def main() -> int:
    site = os.environ.get("WP_URL", "").strip().rstrip("/")
    key = os.environ.get("INDEXNOW_KEY", "").strip()
    if not key:
        print("ℹ️ INDEXNOW_KEY が未設定のため、IndexNow はスキップします。")
        return 0
    if not site:
        sys.exit("❌ WP_URL が未設定です。")

    try:
        r = requests.get(site + SITEMAP_PATH, timeout=TIMEOUT, headers={"User-Agent": "av-indexnow/1.0"})
        r.raise_for_status()
        entries = parse_sitemap(r.text)
    except (requests.RequestException, ET.ParseError) as e:
        print(f"❌ サイトマップを取得・解析できません: {e}", file=sys.stderr)
        return 1

    since = datetime.now(JST).date() - timedelta(days=1)
    urls = pick_recent(entries, since)
    if not urls:
        print(f"ℹ️ {since} 以降に更新されたURLがないため、通知しません。")
        return 0
    host = urlparse(site).netloc
    return 0 if submit(host, key, f"{site}/{key}.txt", urls) else 1


if __name__ == "__main__":
    sys.exit(main())
