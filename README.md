# 大人ナビ（adult-navi21.com）FANZA動画 運用ツール

FANZA動画のランキング・価格・レビューを毎日取得して WordPress の `/av/` に送り込み、更新情報を Bluesky に投稿します。
コードは **`av_hub.py` の1ファイル**にまとめています（サイト側の PHP は別ファイル `av_hub_mu_plugin.php`）。

```
GitHub Actions ─(DMM API → 年齢フィルター → レビュー下限)─▶ WordPress REST (/wp-json/av/v1) ─▶ 専用テーブル
                                                                         │
サイト側 PHP（AVハブ）◀───────────────────────────────────────────────────┘
GitHub Actions ─(サイトの公開データ)─▶ 画像カード作成 ─▶ Bluesky 自動投稿
```

## 1. ファイル構成

| ファイル | 役割 |
|---|---|
| `av_hub.py` | **本体（全機能）。** `sync` / `check` / `cats` / `intro` / `bluesky` / `indexnow` の6コマンド |
| `.github/workflows/av_hub.yml` | 全コマンドを実行する1本のワークフロー（定期実行＋手動実行） |
| `content/categories.json` | 特化カテゴリ（ヘッダーのタブ）の定義 |
| `content/genre_list.csv` / `actress_list.csv` | ジャンル紹介文 / 女優紹介文の記入用テンプレート |
| `tests/test_av_hub.py` | テスト（ネットワーク不要） |
| `requirements.txt` | `requests`（全コマンド）/ `pillow`（Bluesky の画像カード） |
| `av_hub_mu_plugin.php` | サイト側の PHP。`wp-content/mu-plugins/` に置く（本リポジトリの外） |

`av_hub.py` と `requirements.txt` は**リポジトリの直下**に置いてください。

## 2. 初回セットアップ

### 2-1. WordPress 側
1. 設定 → 一般 → **タイムゾーンを「東京」** にする。
2. 設定 → パーマリンク → 「投稿名」など、`?p=123` 以外を選ぶ。
3. `av_hub_mu_plugin.php` を `wp-content/mu-plugins/` に置く（Code Snippets で動かす場合は中身を貼る）。`https://あなたのドメイン/av/` が開けば OK。
4. ユーザー → プロフィール → **アプリケーションパスワード** を作る（**管理者**ユーザーで）。

### 2-2. GitHub 側
このフォルダを新しい**Private**リポジトリに置き、Settings → Secrets and variables → Actions に登録します。

**Secrets**

| 名前 | 用途 | 必須 |
|---|---|---|
| `DMM_API_ID` / `DMM_AFFILIATE_ID` | DMM API | `sync` `cats` |
| `WP_URL` | 例 `https://adult-navi21.com`（末尾スラッシュなし） | `cats` 以外すべて |
| `WP_USERNAME` / `WP_APP_PASSWORD` | 管理者ユーザーとアプリケーションパスワード（24文字） | `sync` `check` `intro` |
| `BSKY_HANDLE` / `BSKY_APP_PASSWORD` | Bluesky（アプリパスワードを使う。通常のパスワードは使わない） | 投稿するとき |
| `DISCORD_WEBHOOK_URL` | 失敗時の通知 | 任意 |

**Variables（すべて任意）**

| 名前 | 意味 | 既定 |
|---|---|---|
| `EXTRA_EXCLUDE` | 取り込みから除外する語（カンマ区切りの正規表現）。「5」を参照 | なし |
| `FOCUS_KEYWORDS` | primary カテゴリ（NTR）の検索語を上書き。`-` でそのカテゴリを取得しない | categories.json の値 |
| `RANK_TOP_N` / `REFRESH_N` | ランキングとして保存する件数（最大100）/ 毎日価格・レビューを更新する人気上位の件数 | 100 / 300 |
| `NEW_DAYS` / `NEW_MAX` | 新着として遡る日数 / 最大件数 | 14 / 300 |
| `BACKFILL_N` | `backfill` の取り込み件数 | 2000 |
| `FOCUS_N` / `FOCUS_BACKFILL_N` / `FOCUS_RANK_N` | カテゴリごとの取り込み件数 / backfill 時 / ランキング件数（カテゴリの `n` `rank_n` が優先） | 200 / 1000 / 100 |
| `BSKY_CATS` | 投稿対象（`ntr,hard` / `all` / `ending` / `rotate`）。空なら全カテゴリ＋終了間近のセール | 空 |
| `BSKY_LABEL` | 自己申告ラベル（`porn` / `sexual` / `nudity` / `graphic-media`） | porn |
| `WEEKLY_HASHTAGS` / `ENDING_HASHTAGS` | カテゴリに指定が無いときのタグ / 終了間近の投稿のタグ | `#FANZA #AV` / `#FANZA #セール` |
| `WEEKLY_MASK_WORDS` | 画像内で伏せたい語（カンマ区切り） | なし |
| `INDEXNOW_KEY` | 設定すると、同期後に IndexNow で更新を通知する | なし |

### 2-3. 動作確認は、この順番で
Actions → **AVハブ（同期・Bluesky・紹介文）** → Run workflow。手動実行の `dry_run` は既定で **true**（送信・投稿しない）です。

| 順 | command | 設定 | 確認すること |
|---|---|---|---|
| 1 | `check` | — | WordPress の接続と認証。失敗すると原因と対処が日本語で出る |
| 2 | `cats` | — | `categories.json` の各カテゴリが DMM API から取れるか |
| 3 | `sync` | `dry_run=true` | 取得とフィルターだけ（WordPress には送らない） |
| 4 | `sync` | `mode=backfill` `dry_run=false` | 初回の大量取り込み。**1回だけ** |
| 5 | `bluesky` | `dry_run=true` | Artifacts（`bluesky-card`）で画像と文章を確認 → 問題なければ `false` |

以降は自動で動きます。「急上昇」はランキングの履歴がたまる**2日目から**表示されます。

## 3. 毎日の運用

| いつ | command | 内容 |
|---|---|---|
| 毎日 05:00 / 17:00 JST | `sync` | 人気上位・新着・カテゴリを取り込み、当日のランキングを保存。`INDEXNOW_KEY` があれば更新を通知 |
| 毎日 06:30 JST | `bluesky` | 急上昇・値下がり・終了間近のセールの画像カードを投稿 |
| 手動 | `intro` | 紹介文CSVの取り込み（「6」を参照） |

同じ種類の処理は同時に1本だけ動きます（ランキングの上書きや二重投稿を防ぐため）。失敗は Discord に通知されます（設定時）。

## 4. カテゴリの設定（`content/categories.json`）

ヘッダーのタブに対応します。`slug` はサイト側の `AV_HUB_CATS` と同じにしてください。

| キー | 意味 |
|---|---|
| `slug` | URL（`/av/{slug}/`）。英小文字・数字・ハイフンの2〜20字。`new` `rank` `sale` など、サイトの別ページで使われている語は不可 |
| `label` | タブ名・投稿の見出し |
| `floor` | `videoa`（ビデオ）/ `videoc`（素人） |
| `keywords` / `genre_name` / （どちらも無し） | 集め方: キーワード検索 / FANZA のジャンル名（IDは自動で引く）/ フロアの全作品 |
| `add_genre` | 取り込む作品にこのジャンル名を付け足す（サイト側がジャンル名でカテゴリを判定するため） |
| `primary` | 従来の特化ジャンル（NTR）に1つだけ付ける |
| `hashtags` | Bluesky 投稿のタグ |
| `n` / `rank_n` | このカテゴリの取り込み件数 / ランキングに記録する件数 |
| `min_reviews` | レビュー件数がこれ未満の作品を取り込まない（0 または省略=制限なし） |
| `fetch_new` | `false` にすると新着を集めない（人気順だけ） |
| `enabled` | `false` にすると、取り込み・ランキング・Bluesky 投稿をすべて止める |

- 素人を「名前だけの作品」で埋めないには、`min_reviews` と `fetch_new: false` を一緒に指定します（現在の設定）。基準は `cats` の「`min_reviews` を満たす作品: x/y件」を見て調整し、**0件になる場合**は 0 に戻してください。
- `enabled: false` にしても、すでに取り込んだ作品はサイトに残ります。タブを消すにはサイト側の `AV_HUB_CATS` も直してください。
- `min_reviews` と `enabled` は、これから取り込む作品にだけ効きます。すでにサイトにある低レビューの作品は、PHP 側の `min_reviews` で素人タブ・トップ・新着などから非表示になります（データは残ります）。

## 5. フィルター

- **取り込み前**（`is_allowed`）: 基本パターンは `小学` の1語だけです。運用中は Variables の `EXTRA_EXCLUDE`（例: `語1,語2`）で語を足す前提です。除外された作品はログに `🛡️ 除外:` と出ます。見逃しより除外しすぎのほうが害が小さいので、迷う語は足してください。
- **SNS 投稿用**（`is_postable`）: 学校・年齢・幼さを連想させるタイトルを、投稿の画像・文章から外します（サイトの掲載には影響しません）。
- **カテゴリの `min_reviews`**: レビュー件数での絞り込み（「4」）。

## 6. 紹介文CSV（ジャンル・女優・メーカー）

検索から入ってくるページに、独自の文章を置くための仕組みです。

- 1行目は見出し。名前の列は、ジャンル=`ジャンル名` / 女優=`女優名` / メーカー=`メーカー名`。
- `説明` 列 = 紹介文の導入。`作品数` 列は使わない（`--top` の並べ替えにだけ使える）。
- それ以外の列は `【列名】値` の行になる。列を足すだけで項目が増える（列名は12文字まで）。
- 文字コードは UTF-8（BOM付き可）か Shift_JIS（Excel の既定）。自動判定。
- 名前は**サイトに表示されている名前と完全に一致**させる（例: `河北彩花（河北彩伽）`）。一致しない行は「見つからなかった」と報告される。

進め方: ① `command=intro` / `type=genre` / `top=20` / `dry_run=true` で確認 → `dry_run=false` で登録。② 女優は `content/actress_list.csv` の空欄を**実際に作品を見て**書き、`type=actress` で同様に実行。

## 7. ローカルで実行する

```bash
pip install -r requirements.txt
export DMM_API_ID=... DMM_AFFILIATE_ID=... \
       WP_URL=https://adult-navi21.com WP_USERNAME=... WP_APP_PASSWORD=...

python av_hub.py check                                  # WordPress の接続診断
python av_hub.py cats                                   # カテゴリの確認
python av_hub.py sync --dry-run                         # 取得とフィルターだけ
python av_hub.py intro --file content/genre_list.csv --top 20 --dry-run
python av_hub.py bluesky --dry-run                      # 画像と文章を out/ に作るだけ（日本語フォントが必要）

python -m unittest discover -s tests -v                 # テスト
```

## 8. DMM のクリック数について

DMM のレポートの「クリック数」は、**アフィリエイトリンク（`al.dmm.co.jp`）がアクセスされた回数**です。

- このリポジトリのコードは `al.dmm.co.jp` にアクセスしません。DMM に送るのは情報取得用の API（`api.dmm.com`）への GET だけで、レポートのクリックには数えられません。したがって、**クリック数は、サイトを見ている人・ボットがリンクを押した（または取得した）分**です。
- DMM 側の数にはボットや検索エンジンのクローラーも含まれます。人のクリックかどうかは、**WordPress 管理画面 → AVハブ →「購入リンクのクリック数（設置場所別・直近14日）」** と比べると推測できます。こちらは JavaScript が動くブラウザでのクリックだけを数え、名乗っているボットは除外します。
  - DMM ≒ 管理画面 → ほぼ人のクリック
  - DMM ≫ 管理画面 → 差分はボット等の可能性が高い（中クリックや広告ブロックで多少ずれるため、完全一致はしません）
- 報酬がほぼ 0 円のままクリックだけが増えている場合も、ボットを疑ってください。

## 9. 困ったとき

| 症状 | 確認すること |
|---|---|
| 同期が 401 / 403 | `check`。原因（認証ヘッダーが落ちている / ユーザー名違い / パスワード違い など）と対処が出る |
| `サイト側にカテゴリ「xxx」がありません` | サイトの AVハブが古い、または `AV_HUB_CATS` に `slug` が未登録。`categories.json` と揃える |
| `ジャンル名「…」のIDを特定できません` | `cats` で候補を確認し、`genre_name` を直す |
| 素人が0件になった | `cats` の「`min_reviews` を満たす作品」を確認。0件なら基準を下げるか 0 にする |
| 「急上昇」が空 | ランキングの履歴が1日分しかない。2日目から出る |
| Bluesky が投稿されない | 急上昇・値下がりのデータが足りない日は投稿しない仕様。`dry_run` のログで理由を確認 |
| `人気順の取得が x/y 件でした` | DMM API の障害の可能性。欠けたランキングは保存せず終了する（翌回に回復を待つ） |
| `av_hub.py` が見つからない | `av_hub.py` と `requirements.txt` がリポジトリの**直下**にあるか |

## 10. 注意

- **DMM アフィリエイトの規約**（画像・サンプル動画の利用条件、表記義務）を必ず確認してください。
- すべてのページに PR 表記を出しています。外さないでください。
- リポジトリは Private にし、アプリケーションパスワードなどは Secrets にだけ置いてください（`.gitignore` で `.env` と `out/` は除外済み）。
- サイト側（PHP）の機能・設定値・変更履歴は、`av_hub_mu_plugin.php` の先頭コメントを参照してください。

## 変更履歴

- **v2.0（単一ファイル化）**: 9個の `.py` を `av_hub.py` に統合し、3本のワークフローを `av_hub.yml` に統合。
  - 削除: Buffer 投稿と画像アップロード（旧 `av_weekly_card.py --post`。旧 API 向けで、ワークフローからは使われていなかった）、週次カードの単体実行、`FOCUS` 環境変数、`rest_route` 経由のフォールバック、`RANK_MIN_RATIO`（0.8 固定）、旧 README の PHP v1.11.0 の説明（現在の PHP は先頭コメントが正）。
  - 共通化: HTTP の再試行（旧: 4か所で別実装）、画像カードの描画（旧: 3関数でほぼ同じコード）、`_as_list` / `_yen` / 正規化の重複、フォントの読み込み（毎回ディスクから読んでいたのをキャッシュ）。
  - 高速化: DMM のジャンル一覧をフロアごとに1回だけ取得（旧: カテゴリごと）、接続の使い回し。
  - 挙動の変更: 認証エラーなどの 4xx は再試行しない（429 / 5xx と通信エラーだけ再試行）。Bluesky は、1つの対象のデータ取得に失敗しても他の対象の投稿を続ける。値下がりだけ投稿できる日の見出しを「値下がり」にした（旧: 「人気TOP0」になりうる）。API キーを含む URL がエラーログに出ないよう伏せる。手動実行の `dry_run` 既定値を `true` にした。
