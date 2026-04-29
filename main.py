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
    get_judgment_stats,
    import_municipalities_from_json,
    insert_judgment,
    get_municipalities,
    record_judgment_outcome,
    update_bid_notified,
)
from filter.matcher import BidMatcher
from judge.llm import BidJudge
from judge.tools import JudgeToolExecutor
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
    parser.add_argument(
        '--feedback', nargs='+', metavar='ARGS',
        help='AI判定にフィードバック記録: --feedback BID_ID applied|skipped|won|lost ["note"]',
    )
    parser.add_argument(
        '--stats', action='store_true',
        help='AI判定ログのサマリ (verdict分布 / フィードバック付き件数 / 精度) を表示',
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

    # 4. AIエージェントによる応募可否判定（LLM + tool use）
    judge = BidJudge(config)
    if judge.is_configured and not args.dry_run:
        logger.info("--- AIエージェント応募可否判定開始 ---")
        # ツール実行用のセッション (HTTP fetch) と DB conn を渡す
        http_session = None
        if judge.use_tools:
            scraper_for_session = (
                municipal_scraper if not args.kkj_only else None
            )
            if scraper_for_session is not None:
                http_session = scraper_for_session.session
            else:
                # スクレイパーが居ない場合は最小限のrequests.Sessionを作る
                import requests
                http_session = requests.Session()
                http_session.headers.update(
                    {'User-Agent': config.get('scraper', {}).get('user_agent', 'MunicipalBidAgent/1.0')}
                )
        executor = JudgeToolExecutor(http_session=http_session, db_conn=conn)
        judged_targets = judge.judge_batch(notify_targets, executor=executor)
        # 判定結果を judgments テーブルに永続化 (フィードバック付与用)
        for bid_dict, _, _, judgment in judged_targets:
            if judgment is not None and not judgment.is_empty:
                try:
                    insert_judgment(conn, bid_dict['id'], judgment, model=judge.model)
                except Exception as e:
                    logger.warning(f"判定ログ保存失敗 (bid_id={bid_dict.get('id')}): {e}")
        apply_count = sum(1 for *_, j in judged_targets if j.verdict == 'apply')
        skip_count = sum(1 for *_, j in judged_targets if j.verdict == 'skip')
        total_tool_calls = sum(j.tool_calls for *_, j in judged_targets if j is not None)
        logger.info(
            f"AI判定完了: 応募推奨={apply_count}, スキップ推奨={skip_count}, "
            f"その他={len(judged_targets) - apply_count - skip_count}, "
            f"ツール呼び出し総数={total_tool_calls}"
        )
    else:
        if not judge.is_configured:
            logger.info("AI判定: 未設定のためスキップ")
        judged_targets = [(b, s, m, None) for b, s, m in notify_targets]

    # 5. 通知
    if not args.scrape_only and not args.dry_run:
        logger.info("--- 通知開始 ---")
        notifier = SlackNotifier(config)

        # 高信頼度 skip 判定の案件は個別通知を抑制 (config.notification.suppress_skip_above_confidence)
        suppress_threshold = (config.get('notification', {}) or {}).get(
            'suppress_skip_above_confidence'
        )
        suppressed_count = 0

        # 個別案件通知
        for bid_dict, score, matched, judgment in judged_targets:
            should_suppress = (
                suppress_threshold is not None
                and judgment is not None
                and not judgment.is_empty
                and judgment.verdict == 'skip'
                and judgment.confidence >= int(suppress_threshold)
            )
            if should_suppress:
                suppressed_count += 1
                update_bid_notified(conn, bid_dict['id'])  # 通知済み扱いにして再判定を防ぐ
                logger.info(
                    f"通知抑制 (skip conf={judgment.confidence}): "
                    f"{(bid_dict.get('title') or '')[:50]}"
                )
                continue
            notifier.notify_bid(bid_dict, score, matched, judgment)
            update_bid_notified(conn, bid_dict['id'])

        if suppressed_count > 0:
            logger.info(f"AI判定 skip 高信頼で {suppressed_count} 件の個別通知を抑制")

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
            'ai_suppressed_count': suppressed_count,
        }
        notifier.notify_summary(summary_stats)

    conn.close()
    logger.info("=== スクレイピング完了 ===")


def record_feedback(args_list):
    """--feedback BID_ID outcome [note] を処理する"""
    logger = logging.getLogger(__name__)
    if len(args_list) < 2:
        print("Usage: --feedback BID_ID outcome [note]", file=sys.stderr)
        print("  outcome: applied | skipped | won | lost", file=sys.stderr)
        sys.exit(2)
    try:
        bid_id = int(args_list[0])
    except ValueError:
        print(f"Error: BID_ID must be integer, got '{args_list[0]}'", file=sys.stderr)
        sys.exit(2)
    outcome = args_list[1]
    note = ' '.join(args_list[2:]) if len(args_list) > 2 else None

    init_db()
    conn = get_connection()
    try:
        n = record_judgment_outcome(conn, bid_id, outcome, note)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        conn.close()
        sys.exit(2)
    if n == 0:
        print(f"No judgment found for bid_id={bid_id}", file=sys.stderr)
        sys.exit(1)
    logger.info(f"Recorded outcome={outcome} for bid_id={bid_id}")
    print(f"OK: bid_id={bid_id} -> {outcome}" + (f" ({note})" if note else ""))
    conn.close()


def show_stats():
    """--stats: 判定ログのサマリを表示する"""
    init_db()
    conn = get_connection()
    s = get_judgment_stats(conn)
    conn.close()

    print("=" * 60)
    print(" AI判定ログ サマリ")
    print("=" * 60)
    print(f" 総判定数:           {s['total']}")
    print(f" verdict内訳:")
    for v in ('apply', 'skip', 'uncertain'):
        print(f"   {v:10s}: {s['by_verdict'].get(v, 0)}")
    print(f" フィードバック済:    {s['with_outcome']}")

    if s['with_outcome'] > 0:
        print(f"\n verdict × outcome 集計:")
        outcomes = ('applied', 'skipped', 'won', 'lost')
        verdicts = ('apply', 'skip', 'uncertain')
        header = '   ' + ''.join(f'{o:>10s}' for o in outcomes)
        print(header)
        for v in verdicts:
            row = f'   {v:8s}'
            for o in outcomes:
                row += f"{s['agreement'].get((v, o), 0):>10d}"
            print(row)
        if s['accuracy'] is not None:
            print(f"\n 単純精度:           {s['accuracy']:.1%}")
            print("   (apply→applied/won, skip→skipped を正解とみなす)")
    else:
        print(" → フィードバックがまだ記録されていません")
        print("   --feedback BID_ID applied|skipped|won|lost で記録してください")
    print("=" * 60)


def main():
    args = parse_args()
    setup_logging()
    config = load_config()

    if args.feedback:
        record_feedback(args.feedback)
        return

    if args.stats:
        show_stats()
        return

    if args.check_urls:
        check_urls(config)
        return

    if args.export:
        export_csv()
        return

    run(args, config)


if __name__ == '__main__':
    main()
