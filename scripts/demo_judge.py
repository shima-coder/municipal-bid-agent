"""デモ用: 既存DBからスコア上位の案件を取り、AIエージェント判定の出力を見る。

使い方:
    ANTHROPIC_API_KEY=sk-ant-... python scripts/demo_judge.py [--limit 5]

config.yaml の llm.enabled に関わらず、このスクリプト内で強制有効化する。
"""

import argparse
import logging
import os
import sys

# プロジェクトルートをパスに
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from db.models import get_connection, init_db
from db.store import import_municipalities_from_json
from judge.llm import BidJudge
from judge.tools import JudgeToolExecutor
from notify.slack import SlackNotifier
from scraper.base import load_config


def main():
    parser = argparse.ArgumentParser(description="AIエージェント判定デモ")
    parser.add_argument("--limit", type=int, default=5, help="判定対象件数 (default 5)")
    parser.add_argument(
        "--min-score", type=int, default=3, help="最低スコア (default 3)"
    )
    parser.add_argument(
        "--no-tools", action="store_true", help="tool useを無効化 (1ショット判定)"
    )
    parser.add_argument(
        "--web-search", action="store_true",
        help="Anthropic web_search server tool を有効化 ($10/1000検索)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("demo")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    # config を読み、強制的に LLM 有効化
    config = load_config()
    config.setdefault("llm", {})
    config["llm"]["enabled"] = True
    config["llm"]["use_tools"] = not args.no_tools
    config["llm"]["enable_web_search"] = args.web_search

    init_db()
    conn = get_connection()
    import_municipalities_from_json(conn)

    # スコア降順で取得 (status関係なし)
    rows = conn.execute(
        """
        SELECT b.id, b.title, b.url, b.bid_type, b.published_date,
               b.filter_score, b.matched_keywords, b.raw_text,
               m.name AS municipality_name, m.prefecture
        FROM bids b
        LEFT JOIN municipalities m ON b.municipality_code = m.code
        WHERE b.filter_score >= ?
        ORDER BY b.filter_score DESC, b.id DESC
        LIMIT ?
        """,
        (args.min_score, args.limit),
    ).fetchall()

    if not rows:
        print(f"No bids with score >= {args.min_score} found")
        sys.exit(0)

    judge = BidJudge(config)
    if not judge.is_configured:
        print("ERROR: BidJudge could not be configured", file=sys.stderr)
        sys.exit(1)

    notifier = SlackNotifier(config)  # webhook空ならコンソール出力

    http_session = requests.Session()
    http_session.headers.update({"User-Agent": "MunicipalBidAgent/1.0 (demo)"})
    executor = JudgeToolExecutor(http_session=http_session, db_conn=conn)

    print("=" * 70)
    print(
        f" 🤖 AIエージェント判定デモ ({len(rows)}件、"
        f"tool_use={judge.use_tools}、web_search={judge.enable_web_search})"
    )
    print(f"    モデル: {judge.model}")
    print("=" * 70)

    for i, row in enumerate(rows, 1):
        bid = dict(row)
        matched = (bid.get("matched_keywords") or "").split(",")
        matched = [k for k in matched if k]

        print(f"\n--- [{i}/{len(rows)}] スコア={bid['filter_score']}: {bid['title'][:60]} ---")
        logger.info(f"判定中: {bid['title'][:50]}")

        judgment = judge.judge(bid, matched, executor=executor)

        print()
        print(notifier.format_bid_message(bid, bid["filter_score"], matched, judgment))
        print(f"\n  [meta] tool_calls={judgment.tool_calls}")

    conn.close()
    print("\n" + "=" * 70)
    print(" デモ完了")
    print("=" * 70)


if __name__ == "__main__":
    main()
