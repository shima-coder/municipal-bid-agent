# Municipal Bid Agent — 自治体入札AIエージェント

四国55町村と官公需ポータル(四国 + 中国地方9県)から入札・プロポーザル公告を毎日収集し、**自社が応募できる案件かどうかをLLMが判定して** Slackに通知する常時稼働エージェント。

NJSS等の有料サービス（年間数万〜十数万円）の代替を、自前のスクレイピング + Claude で構築したもの。

```
[55自治体HP / 官公需ポータル]
        │  毎日スクレイピング (3秒間隔・robots.txt遵守)
        ▼
[ SQLite + url_hash で重複排除 ]
        │
        ▼
[ ルールベースのスコアリング ] ←── キーワード辞書 (高/中/低 + 除外)
        │  しきい値以上の案件のみ次段階へ
        ▼
[ 🤖 AIエージェント (Claude Haiku 4.5 + tool use) ]
        │  ・必要なら fetch_bid_detail で詳細ページを取得
        │  ・必要なら search_past_bids でDB内の同自治体過去案件を検索
        │  ・最大4回までツール呼び出しを繰り返し、判断材料を集めて verdict
        │  応募可否 / 信頼度 / 想定工数 / 懸念点 を JSON で出力
        ▼
[ Slack 通知 ]   ← AI判定結果を含めて配信
```

## なぜ作ったか

- **動機**: 自社（データ分析・BI支援の小規模法人）の案件獲得。NJSSは年間費用が固定費として重い
- **対象セグメント**: 人口5万以下の町村プロポーザル — 大手データ会社が見落としやすく、資格要件も緩い
- **狙い**: 「**機械的なキーワード抽出 → LLMによる文脈判断**」の二段で、ルールでは取りこぼす良案件を拾い、ルールでは弾けないノイズをLLMで切る

## 今動く範囲

- ✅ 四国4県・55町村 + 官公需ポータル (四国4県 + 中国地方5県) のスクレイピング
- ✅ 12項目のキーワード辞書 + プロポーザル種別ボーナスでスコアリング
- ✅ **Tool useでマルチステップ判断**: Claude Haikuがツールを呼んで詳細ページ取得 / 過去案件検索 → `apply` / `skip` / `uncertain` + 理由 + 想定工数を返す
- ✅ プロンプトキャッシュ (system promptを `cache_control: ephemeral`)
- ✅ Slack Incoming Webhookへの個別案件通知 + 日次サマリ
- ✅ SQLiteで差分検知、重複通知なし
- ✅ 162テスト pass、`python main.py --dry-run` で動作確認可能

## 実行デモ (実APIで動かしたログ)

`scripts/demo_judge.py` で Claude Haiku に実際に判定させた結果のサンプル (フルログは [`docs/demo_output.md`](docs/demo_output.md))。

ルールフィルタが「業務委託+プロポーザル」で3点に乗せた案件3件を、AIエージェントが詳細ページを fetch しつつ判定:

```
📋 中土佐町移住・定住総合支援窓口業務委託公募型プロポーザル
🏛️ 中土佐町（高知県）
🔑 キーワード: 業務委託, プロポーザル(種別)

🤖 AI判定: ❌ スキップ推奨（信頼度 72%）
   理由: 移住・定住窓口業務は当社の得意領域（DX/BI/分析）との関連が薄く、
        詳細確認不可。窓口業務委託は通常継続的スタッフ配置を要求され、
        1-2名体制では対応困難の可能性が高い。
   懸念: 窓口業務が主体であれば当社の得意領域外
       / 継続的な人員配置が必要な可能性が高い
   [ツール呼び出し: 2回 (詳細ページfetch)]
```

```
📋 【公募型プロポーザル実施】仁淀川町まちづくり総合計画策定支援業務委託
🤖 AI判定: ❌ スキップ推奨（信頼度 75%）
   理由: まちづくり総合計画策定支援は当社の戦略企画コンサル領域外。
        工数規模も大型の可能性。
   想定工数: 6〜12ヶ月（推定）
```

```
📋 第3次まんのう町健康増進計画及び食育推進計画策定支援業務委託
🤖 AI判定: ❌ スキップ推奨（信頼度 85%）
   理由: 健康増進・食育計画の策定支援は政策企画コンサル領域。
        当社は医療・保健系対象外、かつ計画策定実績なし。
```

**何が嬉しいか**: ルールフィルタ単体だとこれら3件すべて通知に上がってしまう (人間が「これ違うな」と毎回判断する手間)。AI判定が「**窓口業務 = 1-2名体制と不整合**」「**戦略企画コンサル領域**」「**医療分野は対象外**」と当社プロファイルとの不整合を言語化して却下する。

実装メモ:
- 1案件あたり API 呼び出し1-2回 + 詳細ページ fetch (必要時のみ)
- 上記3件のデモ実行コスト ≈ $0.05 (Haiku + prompt caching)

## クイックスタート

```bash
# 依存
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Slack Webhook URL を config.yaml に設定（任意、未設定時はコンソール出力）
# LLM判定を有効化するには下記を設定
export ANTHROPIC_API_KEY=sk-ant-...
# config.yaml の llm.enabled を true に

# 実行
python main.py --dry-run        # スクレイピングなし、スコアリング結果のみ表示
python main.py --kkj-only       # 官公需ポータルのみ
python main.py                  # 通常実行（スクレイピング → 判定 → 通知）

# AI判定にフィードバックを記録 (運用ループ)
python main.py --feedback 4207 skipped "計画策定系で実績不足のため見送り"
python main.py --feedback 1234 applied
python main.py --feedback 1234 won
python main.py --stats          # verdict分布 + outcome集計 + 単純精度

# テスト
python -m pytest tests/ -v
```

## フィードバックループ

判定結果は `judgments` テーブルに永続化され、後から `--feedback` で実結果を
紐付けられる。`--stats` で精度を継続的に追跡:

```
============================================================
 AI判定ログ サマリ
============================================================
 総判定数:           3
 verdict内訳:
   apply     : 0
   skip      : 3
   uncertain : 0
 フィードバック済:    2

 verdict × outcome 集計:
      applied   skipped       won      lost
   apply            0         0         0         0
   skip             0         2         0         0
   uncertain         0         0         0         0

 単純精度:           100.0%
============================================================
```

精度が下がってきたら判定プロンプトやスコアリング閾値を見直す、というループを回せる。

## アーキテクチャ

| Layer | モジュール | 役割 |
|-------|----------|------|
| Scraper | `scraper/base.py`, `scraper/municipal.py`, `scraper/kkj.py` | 自治体HP・官公需ポータルから案件抽出。robots.txt遵守、3秒インターバル、SSL fallback、文字コード自動判定 (Shift_JIS/EUC-JP) |
| Storage | `db/models.py`, `db/store.py` | SQLite。`url_hash`(SHA256) で重複排除。`bids` / `municipalities` / `scrape_logs` の3テーブル |
| Filter | `filter/matcher.py` | キーワード優先度スコアリング (high+3 / mid+2 / low+1 / プロポーザル+2 / 終了済+半減) |
| **Agent** | **`judge/llm.py` + `judge/tools.py`** | **Claude Haikuがツールを使って応募可否を判定するエージェント。`fetch_bid_detail` (HTTPで詳細ページ取得) と `search_past_bids` (DBから過去案件検索) を必要に応じて呼び出す。プロンプトに会社プロファイル + 評価軸を埋め込み、最終的にJSON厳格出力。フェイルセーフ (パース失敗・API失敗・ツール例外で `uncertain` フォールバック)。プロンプトキャッシュでコスト削減** |
| Notify | `notify/slack.py` | Slack Incoming Webhook + コンソールfallback |
| CLI | `main.py` | argparse で `--dry-run` / `--code` / `--kkj-only` / `--check-urls` / `--export` |

## 設計判断

詳細は [`docs/decision.md`](docs/decision.md) に書いてある。要約:

- **なぜLLMを「全件」じゃなく「スコア上位N件」だけに使うか**: コスト制御。スクレイピングで毎日数百〜数千件取得する中、ルールフィルタで2点以上に絞ってからLLM判定。`config.yaml` の `max_judgments_per_run` で上限可変
- **なぜルール + LLM の二段か**: ルールのみだと「住民意識調査」と「データ可視化基盤構築」の文脈差を判別できない。LLMのみだとコスト爆発。役割分担で ROI 最大化
- **なぜ Tool use エージェントにしたか**: 一発のLLM判定だと公告タイトル + 抜粋のみで判断するしかない。`fetch_bid_detail` で詳細ページに踏み込めるようにし、`search_past_bids` で同自治体の過去発注傾向も参照できるようにした。「呼ぶか呼ばないか」もLLMが自分で判断 → 必要な時だけツール使うのでコストも抑えられる
- **なぜ Haiku 4.5 を選ぶか**: 応募可否判定はテキスト読解 + 軽い推論で十分。Sonnet/Opusはコスト見合わない。Tool use loop も Haiku で安定して回る
- **なぜ自治体ごとの個別パーサーを書かないか**: 55自治体のHTML構造を全部書くと保守地獄。汎用パーサー (`<table>` / `<ul><li>` / `<dl>` / `<div>+<a>`) でベストエフォート、取りこぼしは raw_text を残してLLM側でリカバリーする方針
- **なぜローカルSQLiteか**: 個人事業の規模に対してCloud SQLは過剰。差分検知さえ動けば十分。Phase 3で全国展開時に BigQuery 移行予定

## ロードマップ

- Phase 1 (完了): 四国MVP + LLM判定エージェント
- Phase 2: 対象を東北・九州に拡大、cron常駐、落札結果の自動収集
- Phase 3: Cloud Functions + BigQuery 移行、PDF仕様書のエージェント解析（応募書類の下書き自動生成まで）

## ディレクトリ構成

```
municipal-bid-scraper/
├── main.py                 # エントリーポイント
├── config.yaml             # キーワード辞書 / 通知先 / LLM設定
├── data/
│   ├── municipalities.json # 自治体マスタ (55件)
│   └── bids.db             # SQLite (自動生成)
├── scraper/                # ベース + 自治体HP + 官公需ポータル
├── filter/                 # キーワードスコアリング
├── judge/                  # 🤖 AIエージェント
│   ├── llm.py              # BidJudge: tool use ループ + JSON parse
│   └── tools.py            # ツール定義 + 実行 (fetch_bid_detail / search_past_bids)
├── notify/                 # Slack通知
├── db/                     # SQLite モデル / CRUD
├── docs/
│   └── decision.md         # 設計判断ログ
├── tests/                  # 162テスト
└── logs/
```

## 制約と注意

- 自治体サイトは Shift_JIS / EUC-JP / SSL証明書古い等が混在。文字コード自動判定 + verify=False fallback で対応
- リクエスト間隔3秒以上、robots.txt遵守、適切なUser-Agent。礼儀正しいスクレイピング
- 個人情報は一切収集しない
- LLMのJSON出力ブレに対し、コードフェンス除去 + 部分抽出 + verdict正規化 + confidence clamp で防御
