# CLAUDE.md — 自治体入札スクレイピングシステム 設計指示書

## これは何？

自治体HP・官公需ポータルから入札・プロポーザル公告を自動収集し、当社が応募可能な案件を抽出してSlack通知するローカルツール。

**目的**: NJSSなどの有料入札情報サービス（年間数万〜十数万円）を使わずに、自前で案件情報を収集・フィルタリング・通知する。

**ターゲット案件**:
- 自治体の公募型プロポーザル（データ分析・DX・BI関連）
- 予算50万〜500万円の小規模案件
- 資格要件が緩い（実績不問 or 法人格のみ）
- 町村レベル（人口5万以下）が中心

---

## アーキテクチャ

```
[自治体HP群]  →  [Python Scraper]  →  [SQLite]  →  [Filter]  →  [Slack通知]
[官公需ポータル] ↗                                      ↘
                                                  [CSV/JSONエクスポート]
```

- 実行環境: ローカルMac（MacBook Air M4）
- 実行方法: `python main.py`（手動 or cron）
- 実行頻度: 1日1回
- 外部API: Slack Webhook のみ

---

## ディレクトリ構成

```
municipal-bid-scraper/
├── main.py                 # エントリーポイント（CLIオプション付き）
├── config.yaml             # 設定ファイル（キーワード、閾値、通知先）
├── scraper/
│   ├── __init__.py
│   ├── base.py             # ベーススクレイパー（共通処理）
│   ├── municipal.py        # 自治体HP用スクレイパー
│   └── kkj.py              # 官公需ポータル用スクレイパー
├── filter/
│   ├── __init__.py
│   └── matcher.py          # キーワード・条件フィルタリング + スコアリング
├── notify/
│   ├── __init__.py
│   └── slack.py            # Slack Webhook通知
├── db/
│   ├── __init__.py
│   ├── models.py           # SQLiteテーブル定義・マイグレーション
│   └── store.py            # DB操作（CRUD）
├── data/
│   ├── municipalities.json # ターゲット自治体マスタ
│   └── bids.db             # SQLiteデータベース（自動生成）
├── export/
│   └── (CSV/JSONエクスポート先)
├── logs/
│   └── scraper.log
├── tests/
│   ├── test_scraper.py
│   ├── test_filter.py
│   ├── test_notify.py
│   └── test_db.py
├── CLAUDE.md
├── STATE.md
├── requirements.txt
├── config.yaml
└── README.md
```

---

## 技術スタック

- Python 3.12+
- requests + BeautifulSoup4 + lxml（スクレイピング）
- sqlite3（標準ライブラリ、追加パッケージ不要）
- PyYAML（設定ファイル）
- argparse（CLI）
- logging（標準ライブラリ）

---

## 各コンポーネント詳細

### 1. 自治体マスタ (`data/municipalities.json`)

**初期ターゲット: 四国4県（愛媛・高知・香川・徳島）の全町村**

総務省の全国地方公共団体コード一覧から町村を抽出する。
- ソース: https://www.soumu.go.jp/denshijiti/code.html
- 対象: 四国4県の「町」「村」（市・県は除外）
- 各自治体について、HPトップページURLを特定する
- HPから「入札・契約」「プロポーザル」「公告」「新着情報」等のページURLを特定する

```json
[
  {
    "code": "362018",
    "name": "松前町",
    "prefecture": "愛媛県",
    "region": "四国",
    "population": 28000,
    "urls": {
      "top": "https://www.town.masaki.ehime.jp/",
      "bid_page": "https://www.town.masaki.ehime.jp/soshiki/...",
      "news_page": "https://www.town.masaki.ehime.jp/news/..."
    },
    "page_type": "html_list",
    "active": true,
    "notes": ""
  }
]
```

- `page_type`: html_table / html_list / rss / pdf_only / unknown
- `active`: false にするとスクレイピング対象から除外
- 入札ページが存在しない町村 → `active: false`
- robots.txtでdisallow → `active: false` + notesに理由記載
- URLは実際にアクセスして200が返ることを確認すること

### 2. ベーススクレイパー (`scraper/base.py`)

共通処理:
- User-Agent: `MunicipalBidScraper/1.0`
- リクエスト間隔: 同一ドメインに対して最低3秒
- タイムアウト: 30秒
- リトライ: 最大3回（5xx / timeout / ConnectionError）
- robots.txt: 初回アクセス前にチェック、disallowならスキップ
- 文字コード: レスポンスヘッダのcharsetを尊重。なければ`chardet`で推定。Shift_JIS / EUC-JP多い
- SSL: verify=True をデフォルトにしつつ、証明書エラー時はverify=Falseでフォールバック（ログ記録）
- ログ: 全リクエストをscrape_logsテーブルに記録

### 3. 自治体HPスクレイパー (`scraper/municipal.py`)

処理フロー:
1. municipalities.jsonからactive=trueの自治体を取得
2. 各自治体のbid_page / news_pageにアクセス
3. HTMLをパースし、案件情報を抽出
4. SQLiteのbidsテーブルと照合 → 新規（url_hashが未登録）のみ保存

抽出項目:
- title: リンクテキスト or テーブルセルの案件名
- url: 案件詳細ページの絶対URL（相対URLは自動変換）
- published_date: 掲載日（取得できれば）
- bid_type: テキストから推定
  - 「プロポーザル」「企画提案」「企画競争」→ proposal
  - 「入札」「競争入札」→ bid
  - 「随意契約」→ negotiation
  - 不明 → unknown
- raw_text: リンク周辺のテキスト（フィルタリング用）

パーサー戦略:
- 自治体ごとに個別パーサーは書かない
- 汎用パーサーを実装し、以下のパターンをベストエフォートで対応:
  - `<table>` 内の行（最も多い）
  - `<ul><li>` リスト
  - `<div>` + `<a>` の新着情報形式
  - `<dl><dt><dd>` 定義リスト形式
- すべてのパターンを試し、リンク+テキストのペアを抽出
- PDFへの直リンク（.pdf）はタイトルとURLだけ保存

### 4. 官公需ポータルスクレイパー (`scraper/kkj.py`)

- 対象: https://www.kkj.go.jp/s/
- config.yamlのinclude_keywordsを検索クエリとして使用
- 地域フィルタ: 四国4県
- 取得した案件はsource='kkj_portal'としてbidsテーブルに格納
- 自治体HP経由の案件との重複はurl_hashで排除

### 5. フィルタリング (`filter/matcher.py`)

config.yamlから設定を読み込み、スコアリングする:

```yaml
filter:
  include_keywords:
    high_priority:    # +3点
      - データ分析
      - ダッシュボード
      - BI
      - データ可視化
      - DX推進
    medium_priority:  # +2点
      - 統計調査
      - 集計
      - アンケート
      - 効果検証
      - データ基盤
    low_priority:     # +1点
      - 調査業務
      - 報告書作成
      - 業務委託
      - コンサルティング
  exclude_keywords:
    - 工事
    - 建設
    - 設計監理
    - 測量
    - 清掃
    - 給食
    - 医療機器
    - 道路
    - 橋梁
    - 上下水道
    - 警備
    - 印刷
  notify_threshold: 2
```

スコアリングロジック:
1. exclude_keywordsにマッチ → スコア0（除外）
2. include_keywords各カテゴリでマッチ → 加点（複数マッチは加算）
3. bid_type == 'proposal' → +2点ボーナス
4. スコアがnotify_threshold以上 → 通知対象

### 6. Slack通知 (`notify/slack.py`)

個別案件通知（スコアがthreshold以上）:
```
🔔 新着案件: {スコア}点

📋 {案件名}
🏛️ {自治体名}（{都道府県}）
📅 公告日: {公告日 or "不明"}
🏷️ 種別: {プロポーザル / 入札 / 随意契約}
🔑 キーワード: {マッチしたキーワード}
🔗 {案件URL}
```

日次サマリ（毎回実行後に必ず送信）:
```
📊 スクレイピング完了 ({実行日時})

対象自治体: {N}件
取得成功: {N}件 / 失敗: {N}件
新規案件: {N}件（うち通知対象: {N}件）
```

config.yamlの`slack_webhook_url`が空 or 未設定の場合はコンソール出力にフォールバック。

### 7. DB (`db/models.py`, `db/store.py`)

SQLiteテーブル:

```sql
CREATE TABLE IF NOT EXISTS municipalities (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    prefecture TEXT NOT NULL,
    region TEXT,
    population INTEGER,
    bid_page_url TEXT,
    news_page_url TEXT,
    page_type TEXT,
    active BOOLEAN DEFAULT 1,
    last_scraped_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bids (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    municipality_code TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    url_hash TEXT UNIQUE NOT NULL,
    published_date DATE,
    deadline DATE,
    bid_type TEXT DEFAULT 'unknown',
    budget_amount INTEGER,
    source TEXT DEFAULT 'municipal_hp',
    raw_text TEXT,
    filter_score INTEGER DEFAULT 0,
    matched_keywords TEXT,
    status TEXT DEFAULT 'new',
    notified_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (municipality_code) REFERENCES municipalities(code)
);

CREATE TABLE IF NOT EXISTS scrape_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    municipality_code TEXT,
    url TEXT,
    status_code INTEGER,
    success BOOLEAN,
    error_message TEXT,
    items_found INTEGER DEFAULT 0,
    new_items INTEGER DEFAULT 0,
    scraped_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

url_hash = SHA256(url) で重複排除。

### 8. CLI (`main.py`)

```bash
# 通常実行（スクレイピング → フィルタ → 通知）
python main.py

# スクレイピングのみ（通知なし）
python main.py --scrape-only

# 特定自治体のみ
python main.py --code 362018

# ドライラン（DB書き込み・通知なし、コンソール出力のみ）
python main.py --dry-run

# 自治体URLの生存確認
python main.py --check-urls

# DBの案件一覧をCSVエクスポート
python main.py --export csv

# 官公需ポータルのみスクレイピング
python main.py --kkj-only
```

---

## 設計原則

1. **シンプルに保つ**: 外部サービス依存はSlack Webhookのみ。DBはSQLite。
2. **ベストエフォート**: 全案件の完璧な取得は目指さない。取りこぼしは運用で改善。
3. **礼儀正しく**: リクエスト間隔3秒以上、robots.txt遵守、適切なUser-Agent。
4. **段階的に拡張**: まず四国の町村で動かし、後から全国に広げる。

---

## 注意事項

- Shift_JIS / EUC-JPのサイトが多い。文字コード処理に注意
- 自治体サイトはSSL証明書が古い場合がある
- PDFのみで公告している自治体もある（タイトル+URLだけ保存）
- 自治体サイトの構造は統一されていない。汎用パーサーで8割カバーを目指す
- リクエスト間隔は最低3秒。サーバーに負荷をかけない
- 個人情報は一切収集しない

---

## コマンド規則

- テスト: `python -m pytest tests/ -v`
- リント: なし（シンプルに保つ）
- コミットメッセージ: `feat:`, `fix:`, `refactor:`, `docs:`, `test:` プレフィックス
