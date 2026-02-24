# STATE.md — Phase 1: MVP構築

## 現在のPhase: Phase 1
## ステータス: NOT STARTED

---

## ステップ一覧

### Step 1: プロジェクト初期化
- [ ] ディレクトリ構成をCLAUDE.mdに従って作成
- [ ] requirements.txt作成（requests, beautifulsoup4, lxml, pyyaml, chardet）
- [ ] config.yaml作成（キーワード設定、Slack webhook URL空欄）
- [ ] .gitignore作成（data/bids.db, logs/, export/, .env, __pycache__）
- [ ] 各__init__.py作成
- [ ] `pip install -r requirements.txt` で依存確認

### Step 2: データベース構築
- [ ] db/models.py — テーブル定義（CLAUDE.mdのSQLスキーマ通り）
- [ ] db/store.py — CRUD操作（insert_bid, get_bid_by_hash, insert_log, get_municipalities等）
- [ ] DB初期化関数（テーブルなければ自動CREATE）
- [ ] tests/test_db.py — 基本CRUD動作テスト
- [ ] テスト実行して全パス確認

### Step 3: 自治体マスタ作成
- [ ] 総務省の全国地方公共団体コードから四国4県の町村を抽出
  - 愛媛県の町: 松前町, 砥部町, 内子町, 伊方町, 松野町, 鬼北町, 愛南町 等
  - 高知県の町村: 東洋町, 奈半利町, 田野町, 安田町, 北川村, 馬路村, 芸西村, 本山町, 大豊町, 土佐町, 大川村, いの町, 仁淀川町, 中土佐町, 佐川町, 越知町, 梼原町, 日高村, 津野町, 四万十町, 大月町, 三原村, 黒潮町 等
  - 香川県の町: 土庄町, 小豆島町, 三木町, 直島町, 宇多津町, 綾川町, 琴平町, 多度津町, まんのう町 等
  - 徳島県の町村: 勝浦町, 上勝町, 佐那河内村, 石井町, 神山町, 那賀町, 牟岐町, 美波町, 海陽町, 松茂町, 北島町, 藍住町, 板野町, 上板町, つるぎ町, 東みよし町 等
- [ ] 各町村のHPトップページURLをWeb検索で特定
- [ ] 各HPから入札・契約関連ページのURLを特定（実際にアクセスして確認）
  - 「入札」「契約」「プロポーザル」「公告」「新着情報」等のリンクを探す
  - 見つからない場合はnews_pageをトップページの新着情報セクションURLにする
  - ページが存在しない場合は active: false
- [ ] robots.txtを確認（disallowの場合は active: false, notesに記載）
- [ ] data/municipalities.json として保存
- [ ] municipalitiesテーブルにインポートする処理を実装

### Step 4: ベーススクレイパー実装
- [ ] scraper/base.py
  - requests.Session with User-Agent header
  - レート制限（同一ドメイン3秒間隔）— time.sleepベース
  - リトライ（最大3回、exponential backoff）
  - robots.txtチェック（urllib.robotparser使用）
  - 文字コード自動判定（response.encoding → chardet fallback）
  - SSL証明書エラー時のフォールバック（verify=False + warning log）
  - 全リクエストのログ記録（scrape_logsテーブル）

### Step 5: 自治体HPスクレイパー実装
- [ ] scraper/municipal.py
  - municipalities.jsonからactive=trueを取得
  - bid_page, news_page両方をスクレイピング
  - 汎用パーサー実装:
    - `<table>` 行からリンク+テキスト抽出
    - `<ul><li>` からリンク+テキスト抽出
    - `<div>` 内の `<a>` タグ抽出
    - `<dl><dt><dd>` からリンク+テキスト抽出
    - PDF直リンク（href末尾が.pdf）の検出
  - 相対URL → 絶対URL変換（urljoin）
  - bid_type推定（title/raw_textから「プロポーザル」「入札」等を検索）
  - url_hash（SHA256）で重複チェック → 新規のみDB保存
- [ ] tests/test_scraper.py — モックHTMLでパーサーの動作テスト

### Step 6: 官公需ポータルスクレイパー実装
- [ ] scraper/kkj.py
  - https://www.kkj.go.jp/s/ の検索フォームをスクレイピング
  - config.yamlのキーワードで検索
  - 地域フィルタ: 四国（愛媛, 高知, 香川, 徳島）
  - source='kkj_portal' としてDB保存
  - url_hashで自治体HP経由との重複排除

### Step 7: フィルタリング実装
- [ ] filter/matcher.py
  - config.yamlからキーワード設定を読み込み
  - exclude_keywordsチェック → マッチしたらスコア0
  - include_keywords各カテゴリでマッチ → 加点
  - bid_type == 'proposal' → +2点ボーナス
  - スコアとマッチしたキーワードをbidsテーブルに更新
  - notify_threshold以上の案件リストを返す
- [ ] tests/test_filter.py — 各パターンのスコアリングテスト

### Step 8: Slack通知実装
- [ ] notify/slack.py
  - Slack Incoming Webhook POSTリクエスト
  - 個別案件通知フォーマット（CLAUDE.mdの仕様通り）
  - 日次サマリ通知フォーマット
  - webhook_urlが空の場合 → コンソール出力にフォールバック（print）
- [ ] tests/test_notify.py — フォーマット生成テスト（実際の送信はスキップ）

### Step 9: main.py統合
- [ ] argparseでCLIオプション実装:
  - `--scrape-only`: スクレイピングのみ（通知なし）
  - `--code CODE`: 特定自治体コードのみ実行
  - `--dry-run`: DB書き込み・通知なし、コンソール出力のみ
  - `--check-urls`: 自治体URLの生存確認（200チェック）
  - `--export csv`: bidsテーブルをCSVエクスポート（export/ディレクトリ）
  - `--kkj-only`: 官公需ポータルのみ
- [ ] 通常実行フロー:
  1. DB初期化
  2. 自治体HPスクレイピング
  3. 官公需ポータルスクレイピング
  4. 新規案件にフィルタリング適用
  5. 閾値以上の案件をSlack通知
  6. 日次サマリ通知
  7. ログ出力
- [ ] logging設定（ファイル + コンソール）

### Step 10: 統合テスト・README
- [ ] 全テスト実行 `python -m pytest tests/ -v` → 全パス
- [ ] `python main.py --dry-run` で一連のフローが動くことを確認
- [ ] README.md作成（セットアップ手順、使い方、config.yamlの説明）
- [ ] git commit

---

## 完了条件

- [ ] `python main.py --dry-run` で四国の町村をスクレイピングし、案件一覧がコンソールに出力される
- [ ] フィルタリングが動作し、スコア付きの案件が表示される
- [ ] Slack webhook URL設定時に通知が送信される
- [ ] 2回目の実行で差分検知が動き、既存案件は重複登録されない
- [ ] テストが全パスする

---

## 次Phase予告

Phase 2（手動トリガー）:
- 対象自治体を東北・九州に拡大（50-100件）
- cron設定で毎朝自動実行
- Looker Studio連携（SQLite → CSV → スプシ → Looker）
- 落札結果の自動収集

Phase 3（将来）:
- Cloud Functions移行
- BigQuery移行
- Claude APIによる仕様書PDF自動分析・応募可否判定
