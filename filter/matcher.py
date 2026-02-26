"""キーワード・条件フィルタリング + スコアリング"""

import logging

from scraper.base import load_config
from db.store import update_bid_score

logger = logging.getLogger(__name__)

# キーワード優先度ごとの加点
PRIORITY_SCORES = {
    'high_priority': 3,
    'medium_priority': 2,
    'low_priority': 1,
}

PROPOSAL_BONUS = 2

# 終了済み案件キーワード（スコア半減）
RESULT_KEYWORDS = ['審査結果', '結果公表', '選定結果', '実施結果', '実施しました', '公表します', '結果について']


class BidMatcher:
    """案件のキーワードフィルタリング・スコアリング"""

    def __init__(self, config=None):
        if config is None:
            config = load_config()
        filter_config = config.get('filter', {})

        self.include_keywords = filter_config.get('include_keywords', {})
        self.exclude_keywords = filter_config.get('exclude_keywords', [])
        self.notify_threshold = filter_config.get('notify_threshold', 2)

    def score_bid(self, bid):
        """案件をスコアリングし、(score, matched_keywords) を返す。

        Args:
            bid: dict-like object with keys 'title', 'raw_text', 'bid_type'

        Returns:
            tuple: (score: int, matched_keywords: list[str])
        """
        if not isinstance(bid, dict):
            bid = dict(bid)
        title = bid.get('title', '') or ''
        raw_text = bid.get('raw_text', '') or ''
        bid_type = bid.get('bid_type', '') or ''
        url = bid.get('url', '') or ''
        search_text = f"{title} {raw_text}"

        # URLが#main_pageで終わる → 除外
        if url.endswith('#main_page'):
            logger.debug(f"Excluded by #main_page URL: {title}")
            return 0, []

        # タイトルが5文字以下 → 除外
        if len(title) <= 5:
            logger.debug(f"Excluded by short title ({len(title)} chars): {title}")
            return 0, []

        # exclude_keywordsチェック
        for keyword in self.exclude_keywords:
            if keyword in search_text:
                logger.debug(f"Excluded by keyword '{keyword}': {title}")
                return 0, []

        # include_keywordsで加点
        score = 0
        matched = []
        for priority, keywords in self.include_keywords.items():
            points = PRIORITY_SCORES.get(priority, 1)
            for keyword in keywords:
                if keyword in search_text:
                    score += points
                    matched.append(keyword)

        # bid_type == 'proposal' ボーナス
        if bid_type == 'proposal':
            score += PROPOSAL_BONUS
            matched.append('プロポーザル(種別)')

        # 終了済み案件（審査結果・選定結果等）→ スコア半減
        for kw in RESULT_KEYWORDS:
            if kw in title:
                original_score = score
                score = score // 2
                logger.debug(
                    f"Score halved by '{kw}' ({original_score} -> {score}): {title}"
                )
                break

        return score, matched

    def filter_bids(self, bids):
        """案件リストをスコアリングし、notify_threshold以上の案件を返す。

        Args:
            bids: list of dict-like objects

        Returns:
            list of (bid, score, matched_keywords) tuples
        """
        results = []
        for bid in bids:
            score, matched = self.score_bid(bid)
            if score >= self.notify_threshold:
                results.append((bid, score, matched))
        # スコア降順でソート
        results.sort(key=lambda x: x[1], reverse=True)
        return results

    def apply_to_new_bids(self, conn, bids):
        """新規案件にスコアリングを適用し、DBを更新する。
        notify_threshold以上の案件リストを返す。

        Args:
            conn: SQLite connection
            bids: list of dict-like objects (Row objects from DB)

        Returns:
            list of (bid, score, matched_keywords) tuples
        """
        notify_targets = []
        for bid in bids:
            bid_dict = dict(bid) if not isinstance(bid, dict) else bid
            score, matched = self.score_bid(bid_dict)
            matched_str = ','.join(matched) if matched else ''

            update_bid_score(conn, bid_dict['id'], score, matched_str)

            if score >= self.notify_threshold:
                notify_targets.append((bid_dict, score, matched))
                logger.info(
                    f"Notify target (score={score}): {bid_dict.get('title', '')}"
                )

        notify_targets.sort(key=lambda x: x[1], reverse=True)
        return notify_targets
