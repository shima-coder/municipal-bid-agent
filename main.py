"""自治体入札スクレイピングシステム — エントリーポイント"""

import argparse
import csv
import logging
import os
import sys
from datetime import datetime

from db.models import init_db, get_connection
from db.store import (
    get_all_bids,
    get_bids_by_status,
    import_municipalities_from_json,
    get_municipalities,
    update_bid_notified,
)
from filter.matcher import BidMatcher
from notify.slack import SlackNotifier
from scraper.base import load_config
from scraper.municipal import MunicipalScraper
from scraper.kkj import KKJScraper


def setup_logging():
    """ログ設定（ファイル + コンソール）"""
    log_dir = os.path.join(os.path.dirname(__file__), 'logs')
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'scraper.log')

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler(sys.stdout),
        ],
    )


def parse_args():
    """CLIオプションをパースする"""
    parser = argparse.ArgumentParser(
        description='自治体入札・プロポーザル情報スクレイピングツール'
    )
    parser.add_argument(
        '--scrape-only', action='store_true',
        help='スクレイピングのみ（通知なし）',
    )
    parser.add_argument(
        '--code', type=str, default=None,
        help='特定自治体コードのみ実行',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='DB書き込み・通知なし、コンソール出力のみ',
    )
    parser.add_argument(
        '--check-urls', action='store_true',
        help='自治体URLの生存確認',
    )
    parser.add_argument(
        '--export', type=str, choices=['csv'], default=None,
        help='bidsテーブルをエクスポート',
    )
    parser.add_argument(
        '--kkj-only', action='store_true',
        help='官公需ポータルのみスクレイピング',
    )
    return parser.parse_args()


def check_urls(config):
    """自治体URLの生存確認"""
    logger = logging.getLogger(__name__)
    conn = get_connection()
    init_db()
    import_municipalities_from_json(conn)
    municipalities = get_municipalities(conn, active_only=True)

    scraper = MunicipalScraper(config)
    ok_count = 0
    fail_count = 0

    for m in municipalities:
        urls = []
        if m['bid_page_url']:
            urls.append(('bid_page', m['bid_page_url']))
        if m['news_page_url']:
            urls.append(('news_page', m['news_page_url']))

        for url_type, url in urls:
            try:
                response = scraper.session.get(url, timeout=10, verify=True)
                status = response.status_code
            except Exception as e:
                status = str(e)

            if isinstance(status, int) and status == 200:
                ok_count += 1
                logger.info(f"OK  {m['name']} ({m['code']}) {url_type}: {url}")
            else:
                fail_count += 1
                logger.warning(
                    f"FAIL {m['name']} ({m['code']}) {url_type}: {url} -> {status}"
                )

    logger.info(f"URL check complete: OK={ok_count}, FAIL={fail_count}")
    conn.close()


def export_csv():
    """bidsテーブルをCSVエクスポート"""
    logger = logging.getLogger(__name__)
    conn = get_connection()
    init_db()

    bids = get_all_bids(conn)
    if not bids:
        logger.info("No bids to export")
        conn.close()
        return

    export_dir = os.path.join(os.path.dirname(__file__), 'export')
    os.makedirs(export_dir, exist_ok=True)
    filename = f"bids_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    filepath = os.path.join(export_dir, filename)

    fieldnames = [
        'id', 'municipality_code', 'municipality_name', 'prefecture',
        'title', 'url', 'published_date', 'deadline', 'bid_type',
        'budget_amount', 'source', 'filter_score', 'matched_keywords',
        'status', 'created_at',
    ]

    with open(filepath, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for bid in bids:
            row = {k: bid[k] for k in fieldnames if k in bid.keys()}
            writer.writerow(row)

    logger.info(f"Exported {len(bids)} bids to {filepath}")
    conn.close()


def run(args, config):
    """メイン実行フロー"""
    logger = logging.getLogger(__name__)
    logger.info("=== スクレイピング開始 ===")

    # 1. DB初期化 + 自治体マスタインポート
    init_db()
    conn = get_connection()
    import_municipalities_from_json(conn)

    municipal_result = None
    kkj_result = None

    # 2. スクレイピング
    if args.dry_run:
        logger.info("[DRY-RUN] スクレイピングをスキップ")
    else:
        if not args.kkj_only:
            # 自治体HPスクレイピング
            logger.info("--- 自治体HPスクレイピング開始 ---")
            municipal_scraper = MunicipalScraper(config)
            municipal_result = municipal_scraper.scrape_all(
                conn, target_code=args.code
            )
            logger.info(
                f"自治体HP: 対象={municipal_result['total_municipalities']}, "
                f"成功={municipal_result['success']}, "
                f"失敗={municipal_result['failed']}, "
                f"新規={municipal_result['total_new']}"
            )

        if not args.code:
            # 官公需ポータルスクレイピング
            logger.info("--- 官公需ポータルスクレイピング開始 ---")
            kkj_scraper = KKJScraper(config)
            kkj_result = kkj_scraper.scrape_all(conn)
            logger.info(
                f"官公需: キーワード={kkj_result['total_keywords']}, "
                f"新規={kkj_result['total_new']}"
            )

    # 3. フィルタリング
    logger.info("--- フィルタリング開始 ---")
    matcher = BidMatcher(config)
    new_bids = get_bids_by_status(conn, 'new')

    if args.dry_run:
        # ドライラン: スコアリング結果をコンソール出力のみ
        notify_targets = matcher.filter_bids(new_bids)
        logger.info(f"[DRY-RUN] 新規案件: {len(new_bids)}件, 通知対象: {len(notify_targets)}件")
        for bid, score, matched in notify_targets:
            bid_dict = dict(bid) if not isinstance(bid, dict) else bid
            print(
                f"  [{score}点] {bid_dict.get('title', '')} "
                f"({bid_dict.get('bid_type', 'unknown')}) "
                f"キーワード: {', '.join(matched)}"
            )
            print(f"         URL: {bid_dict.get('url', '')}")
    else:
        notify_targets = matcher.apply_to_new_bids(conn, new_bids)
        logger.info(f"新規案件: {len(new_bids)}件, 通知対象: {len(notify_targets)}件")

    # 4. 通知
    if not args.scrape_only and not args.dry_run:
        logger.info("--- 通知開始 ---")
        notifier = SlackNotifier(config)

        # 個別案件通知
        for bid_dict, score, matched in notify_targets:
            notifier.notify_bid(bid_dict, score, matched)
            update_bid_notified(conn, bid_dict['id'])

        # 日次サマリ通知
        total_municipalities = 0
        success_count = 0
        failure_count = 0
        total_new = 0

        if municipal_result:
            total_municipalities += municipal_result['total_municipalities']
            success_count += municipal_result['success']
            failure_count += municipal_result['failed']
            total_new += municipal_result['total_new']
        if kkj_result:
            total_new += kkj_result['total_new']

        summary_stats = {
            'total_municipalities': total_municipalities,
            'success_count': success_count,
            'failure_count': failure_count,
            'total_new_items': total_new,
            'notify_count': len(notify_targets),
        }
        notifier.notify_summary(summary_stats)

    conn.close()
    logger.info("=== スクレイピング完了 ===")


def main():
    args = parse_args()
    setup_logging()
    config = load_config()

    if args.check_urls:
        check_urls(config)
        return

    if args.export:
        export_csv()
        return

    run(args, config)


if __name__ == '__main__':
    main()
