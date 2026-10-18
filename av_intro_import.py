# -*- coding: utf-8 -*-
"""
CSV（Excelで作った一覧）から、ジャンル/女優/メーカーの紹介文を WordPress（AVハブ）へまとめて登録する。

  python av_intro_import.py --file content/genre_list.csv --dry-run          # 照合結果の確認だけ（書き込まない）
  python av_intro_import.py --file content/genre_list.csv                    # 登録（記入済みの紹介文は上書きしない）
  python av_intro_import.py --file content/genre_list.csv --overwrite        # 記入済みも上書きする

CSVの規則:
  ・1行目は見出し。名前の列は、ジャンル=「ジャンル名」/ 女優=「女優名」/ メーカー=「メーカー名」（--name-col で変更可）
  ・「説明」列 = 紹介文の導入文（最初の段落）
  ・「作品数」列 = 使わない（--ignore で変更可）。当サイトの件数は自動で表示されるため
  ・それ以外の列 = 【列名】値 のラベル付きの行になる（例: 「良いところ」→ 【良いところ】…）。
    列を足すだけで項目を増やせる（例: 「合わない人」「入口作品」）。列名は12文字まで。
  ・文字コードは UTF-8（BOM付き可）または Shift_JIS（Excelの既定のCSV）を自動判定

必須の環境変数: WP_URL / WP_USERNAME / WP_APP_PASSWORD （DMMのキーは不要）
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
from pathlib import Path
from typing import Any

from age_filter import is_allowed
from av_sync import WpClient, load_config

NAME_COLS = {"genre": "ジャンル名", "actress": "女優名", "maker": "メーカー名"}
BATCH = 100
MAX_LABEL = 12   # 【ラベル】の最大文字数（サイト側の整形ルールに合わせる）


def read_csv(path: Path) -> list[dict[str, str]]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp932"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise SystemExit("❌ 文字コードを判定できません（UTF-8 か Shift_JIS で保存してください）")
    rows = list(csv.DictReader(io.StringIO(text, newline="")))
    if not rows:
        raise SystemExit("❌ CSVにデータ行がありません")
    return [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in rows]


def build_intro(row: dict[str, str], name_col: str, lead_col: str, ignore: set[str]) -> str:
    """導入文 + 【ラベル】行 の紹介文を組み立てる（サイト側がこの形式を読みやすく整形する）。"""
    lead = " ".join(row.get(lead_col, "").split())
    if lead and not lead.endswith(("。", "！", "？", ".", "!", "?", "）", ")")):
        lead += "。"
    labeled = []
    for col, val in row.items():
        if col in (name_col, lead_col) or col in ignore or not col:
            continue
        val = " ".join(val.split())      # セル内の改行・連続空白は1つの空白に
        if val:
            labeled.append(f"【{col}】{val}")
    parts = [p for p in ("\n".join([lead]) if lead else "", "\n".join(labeled)) if p]
    return "\n\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="CSVから紹介文をまとめて登録する")
    ap.add_argument("--file", required=True, help="CSVのパス")
    ap.add_argument("--type", choices=tuple(NAME_COLS), default="genre")
    ap.add_argument("--name-col", help="名前の列の見出し（既定: 種類に応じて ジャンル名/女優名/メーカー名）")
    ap.add_argument("--lead-col", default="説明", help="導入文にする列（既定: 説明）")
    ap.add_argument("--ignore", default="作品数", help="使わない列（カンマ区切り。既定: 作品数）")
    ap.add_argument("--overwrite", action="store_true", help="記入済みの紹介文も上書きする")
    ap.add_argument("--dry-run", action="store_true", help="照合結果だけ確認し、書き込まない")
    args = ap.parse_args()

    rows = read_csv(Path(args.file))
    name_col = args.name_col or NAME_COLS[args.type]
    if name_col not in rows[0]:
        print(f"⚠️ 見出し『{name_col}』が無いため、1列目『{next(iter(rows[0]))}』を名前として使います。")
        name_col = next(iter(rows[0]))
    ignore = {c.strip() for c in args.ignore.split(",") if c.strip()}

    # 見出し（ラベル）の長さチェック
    labels = [c for c in rows[0] if c and c not in (name_col, args.lead_col) and c not in ignore]
    too_long = [c for c in labels if len(c) > MAX_LABEL]
    if too_long:
        print(f"❌ 列名が{MAX_LABEL}文字を超えています: {too_long}", file=sys.stderr)
        return 1
    print(f"📄 {args.file}: {len(rows)}行 / 名前の列=『{name_col}』 導入文=『{args.lead_col}』 ラベル行=『{'』『'.join(labels) or 'なし'}』")

    # 取り込み前の安全確認（未成年を連想させる語、EXTRA_EXCLUDE の語）と、重複・空欄の除外
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    blocked: list[tuple[str, str]] = []
    skipped_empty = dup = 0
    for r in rows:
        name = r.get(name_col, "")
        intro = build_intro(r, name_col, args.lead_col, ignore)
        if not name or not intro:
            skipped_empty += 1
            continue
        if name in seen:
            dup += 1
            continue
        ok, hits = is_allowed(name, intro)
        if not ok:
            blocked.append((name, hits[0]))
            continue
        seen.add(name)
        items.append({"name": name, "intro": intro})
    if blocked:
        print(f"🛡️ 安全フィルターで除外 {len(blocked)}件: " + "、".join(f"{n}" for n, _ in blocked))
    if skipped_empty or dup:
        print(f"⚠️ 空欄で除外 {skipped_empty}件 / 重複で除外 {dup}件")
    if not items:
        print("❌ 登録できる行がありません", file=sys.stderr)
        return 1
    print(f"👀 プレビュー（先頭1件）: 【{items[0]['name']}】\n" + "\n".join("    " + ln for ln in items[0]["intro"].split("\n")))

    cfg = load_config(need_wp=True, need_dmm=False)
    wp = WpClient(cfg)
    total: dict[str, Any] = {"updated": 0, "would_update": 0, "unchanged": 0, "invalid": 0}
    not_found: list[str] = []
    skipped: list[str] = []
    failed = 0
    for i in range(0, len(items), BATCH):
        batch = items[i:i + BATCH]
        try:
            res = wp.post("intros", {"type": args.type, "items": batch, "overwrite": args.overwrite, "dry_run": args.dry_run})
        except RuntimeError as e:
            failed += len(batch)
            print(f"❌ {e}", file=sys.stderr)
            continue
        for k in total:
            total[k] += int(res.get(k, 0))
        not_found += res.get("not_found", [])
        skipped += res.get("skipped_existing", [])

    mode = "（確認のみ・書き込みなし）" if args.dry_run else ""
    print(f"\n===== 結果{mode} =====")
    if args.dry_run:
        print(f"登録される予定: {total['would_update']}件")
    else:
        print(f"登録した: {total['updated']}件")
    print(f"変更なし（すでに同じ内容）: {total['unchanged']}件")
    print(f"記入済みのため上書きしなかった: {len(skipped)}件" + (f"  → {'、'.join(skipped[:30])}{'…' if len(skipped) > 30 else ''}" if skipped else ""))
    print(f"サイトに見つからなかった: {len(not_found)}件" + (f"  → {'、'.join(not_found[:40])}{'…' if len(not_found) > 40 else ''}" if not_found else ""))
    if failed:
        print(f"❌ 送信に失敗: {failed}件", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
