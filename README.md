# 自治体入札スクレイピングシステム

自治体HP・官公需ポータルから入札・プロポーザル公告を自動収集し、応募可能な案件を抽出してSlack通知するツール。

## 対象

- 四国4県（愛媛・高知・香川・徳島）の全町村（55自治体）
- 官公需ポータル（https://www.kkj.go.jp/s/）
- データ分析・DX・BI関連の小規模プロポーザル案件

## セットアップ

```bash
# Python 3.12+ が必要
python --version

# 依存パッケージのインストール
pip install -r requirements.txt

# config.yaml を編集（Slack通知を使う場合）
# notification.slack_webhook_url にWebhook URLを設定
```

## 使い方

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

## config.yaml

```yaml
filter:
  include_keywords:
    high_priority:    # +3点
      - データ分析
      - ダッシュボード
    medium_priority:  # +2点
      - 統計調査
      - アンケート
    low_priority:     # +1点
      - 調査業務
      - 業務委託
  exclude_keywords:
    - 工事
    - 建設
  notify_threshold: 2  # この点数以上で通知

notification:
  slack_webhook_url: ""  # 空の場合はコンソール出力

scraper:
  request_interval: 3  # 秒
  timeout: 30
  max_retries: 3
  user_agent: "MunicipalBidScraper/1.0"
```

## Slack Webhook設定

1. https://api.slack.com/apps でアプリを作成
2. **Incoming Webhooks** を有効化
3. ワークスペースにインストールし、チャンネルを選択
4. Webhook URLをコピー
5. `config.yaml` の `notification.slack_webhook_url` に貼り付け

## cron設定例

毎朝8時に自動実行:

```bash
# crontab -e
0 8 * * * cd /path/to/municipal-bid-scraper && /path/to/python main.py >> logs/cron.log 2>&1
```

## テスト

```bash
python -m pytest tests/ -v
```

## ディレクトリ構成

```
municipal-bid-scraper/
├── main.py              # エントリーポイント
├── config.yaml          # 設定ファイル
├── scraper/
│   ├── base.py          # ベーススクレイパー（共通処理）
│   ├── municipal.py     # 自治体HP用スクレイパー
│   └── kkj.py           # 官公需ポータル用スクレイパー
├── filter/
│   └── matcher.py       # キーワードフィルタリング・スコアリング
├── notify/
│   └── slack.py         # Slack通知
├── db/
│   ├── models.py        # SQLiteテーブル定義
│   └── store.py         # DB操作
├── data/
│   ├── municipalities.json  # 自治体マスタ（55件）
│   └── bids.db              # SQLiteデータベース（自動生成）
├── tests/               # テスト（137件）
└── logs/                # ログ出力先
```
