"""フィルタリング・スコアリングのテスト"""

import os
import tempfile
import pytest

from db.models import init_db, get_connection
from db.store import insert_bid, get_bid_by_hash, url_hash
from filter.matcher import BidMatcher, PRIORITY_SCORES, PROPOSAL_BONUS


@pytest.fixture
def config():
    """テスト用のconfig"""
    return {
        'filter': {
            'include_keywords': {
                'high_priority': ['データ分析', 'ダッシュボード', 'BI'],
                'medium_priority': ['統計調査', '集計', 'アンケート'],
                'low_priority': ['調査業務', '報告書作成', '業務委託'],
            },
            'exclude_keywords': ['工事', '建設', '測量', '清掃'],
            'notify_threshold': 2,
        }
    }


@pytest.fixture
def matcher(config):
    return BidMatcher(config=config)


@pytest.fixture
def db_conn():
    """テスト用の一時DB"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, 'test.db')
        init_db(db_path)
        conn = get_connection(db_path)
        yield conn
        conn.close()


def make_bid(title='テスト案件', raw_text='', bid_type='unknown',
             url='https://example.com/bid/1', municipality_code='362018'):
    return {
        'title': title,
        'raw_text': raw_text,
        'bid_type': bid_type,
        'url': url,
        'municipality_code': municipality_code,
        'published_date': None,
        'deadline': None,
        'budget_amount': None,
        'source': 'municipal_hp',
    }


# --- score_bid テスト ---

class TestScoreBid:

    def test_exclude_keyword_returns_zero(self, matcher):
        bid = make_bid(title='道路工事の業務委託')
        score, matched = matcher.score_bid(bid)
        assert score == 0
        assert matched == []

    def test_exclude_in_raw_text(self, matcher):
        bid = make_bid(title='業務委託', raw_text='建設に関する案件')
        score, matched = matcher.score_bid(bid)
        assert score == 0

    def test_high_priority_keyword(self, matcher):
        bid = make_bid(title='データ分析業務の委託について')
        score, matched = matcher.score_bid(bid)
        assert 'データ分析' in matched
        assert score >= PRIORITY_SCORES['high_priority']

    def test_medium_priority_keyword(self, matcher):
        bid = make_bid(title='住民アンケート集計業務')
        score, matched = matcher.score_bid(bid)
        assert 'アンケート' in matched
        assert '集計' in matched

    def test_low_priority_keyword(self, matcher):
        bid = make_bid(title='調査業務の報告書作成')
        score, matched = matcher.score_bid(bid)
        assert '調査業務' in matched
        assert '報告書作成' in matched
        assert score == PRIORITY_SCORES['low_priority'] * 2

    def test_multiple_priority_levels(self, matcher):
        bid = make_bid(title='データ分析による統計調査')
        score, matched = matcher.score_bid(bid)
        expected = PRIORITY_SCORES['high_priority'] + PRIORITY_SCORES['medium_priority']
        assert score == expected
        assert 'データ分析' in matched
        assert '統計調査' in matched

    def test_proposal_bonus(self, matcher):
        bid = make_bid(title='一般的な案件', bid_type='proposal')
        score, matched = matcher.score_bid(bid)
        assert score == PROPOSAL_BONUS
        assert 'プロポーザル(種別)' in matched

    def test_keyword_plus_proposal_bonus(self, matcher):
        bid = make_bid(title='データ分析プロポーザル', bid_type='proposal')
        score, matched = matcher.score_bid(bid)
        expected = PRIORITY_SCORES['high_priority'] + PROPOSAL_BONUS
        assert score == expected

    def test_no_match_returns_zero(self, matcher):
        bid = make_bid(title='全く関係ない案件のお知らせ')
        score, matched = matcher.score_bid(bid)
        assert score == 0
        assert matched == []

    def test_empty_title_and_raw_text(self, matcher):
        bid = make_bid(title='', raw_text='')
        score, matched = matcher.score_bid(bid)
        assert score == 0

    def test_none_values_handled(self, matcher):
        bid = {'title': None, 'raw_text': None, 'bid_type': None}
        score, matched = matcher.score_bid(bid)
        assert score == 0
        assert matched == []

    def test_keyword_in_raw_text(self, matcher):
        bid = make_bid(title='業務委託について', raw_text='BI導入に関する業務')
        score, matched = matcher.score_bid(bid)
        assert 'BI' in matched
        assert '業務委託' in matched

    def test_exclude_takes_priority_over_include(self, matcher):
        """除外キーワードと包含キーワードが両方マッチした場合、除外が優先"""
        bid = make_bid(title='建設データ分析業務')
        score, matched = matcher.score_bid(bid)
        assert score == 0


# --- filter_bids テスト ---

class TestFilterBids:

    def test_filters_above_threshold(self, matcher):
        bids = [
            make_bid(title='データ分析業務'),  # high=3, >= 2
            make_bid(title='何かの案件', url='https://example.com/2'),  # 0, < 2
            make_bid(title='統計調査とアンケート', url='https://example.com/3'),  # medium=2+2=4, >= 2
        ]
        results = matcher.filter_bids(bids)
        assert len(results) == 2

    def test_results_sorted_by_score_desc(self, matcher):
        bids = [
            make_bid(title='調査業務', url='https://example.com/1'),  # low=1
            make_bid(title='データ分析ダッシュボード', url='https://example.com/2'),  # high=3+3=6
            make_bid(title='統計調査の業務委託', url='https://example.com/3'),  # medium=2 + low=1 = 3
        ]
        results = matcher.filter_bids(bids)
        scores = [r[1] for r in results]
        assert scores == sorted(scores, reverse=True)
        assert results[0][1] == 6  # データ分析ダッシュボード

    def test_empty_list(self, matcher):
        results = matcher.filter_bids([])
        assert results == []

    def test_all_excluded(self, matcher):
        bids = [
            make_bid(title='道路工事'),
            make_bid(title='建設資材', url='https://example.com/2'),
        ]
        results = matcher.filter_bids(bids)
        assert len(results) == 0

    def test_threshold_exact_match(self, matcher):
        """スコアがちょうどthresholdの場合は通知対象に含まれる"""
        bid = make_bid(title='統計調査についての案件')  # medium=2, threshold=2
        results = matcher.filter_bids([bid])
        assert len(results) == 1
        assert results[0][1] == 2


# --- apply_to_new_bids テスト ---

class TestApplyToNewBids:

    def _insert_test_bid(self, conn, title='テスト', raw_text='',
                         bid_type='unknown', url='https://example.com/bid/1'):
        bid = make_bid(title=title, raw_text=raw_text,
                       bid_type=bid_type, url=url)
        insert_bid(conn, bid)
        return get_bid_by_hash(conn, url_hash(url))

    def test_updates_db_score(self, matcher, db_conn):
        row = self._insert_test_bid(db_conn, title='データ分析業務')
        matcher.apply_to_new_bids(db_conn, [row])

        updated = get_bid_by_hash(db_conn, url_hash('https://example.com/bid/1'))
        assert updated['filter_score'] == PRIORITY_SCORES['high_priority']
        assert 'データ分析' in updated['matched_keywords']

    def test_returns_notify_targets(self, matcher, db_conn):
        row = self._insert_test_bid(db_conn, title='BI導入ダッシュボード構築')
        results = matcher.apply_to_new_bids(db_conn, [row])
        assert len(results) == 1
        assert results[0][1] == PRIORITY_SCORES['high_priority'] * 2

    def test_excludes_below_threshold(self, matcher, db_conn):
        row = self._insert_test_bid(db_conn, title='一般的な案件')
        results = matcher.apply_to_new_bids(db_conn, [row])
        assert len(results) == 0

    def test_multiple_bids(self, matcher, db_conn):
        row1 = self._insert_test_bid(db_conn, title='データ分析業務',
                                     url='https://example.com/1')
        row2 = self._insert_test_bid(db_conn, title='一般的な案件',
                                     url='https://example.com/2')
        row3 = self._insert_test_bid(db_conn, title='アンケート集計業務',
                                     url='https://example.com/3')
        results = matcher.apply_to_new_bids(db_conn, [row1, row2, row3])
        assert len(results) == 2


# --- BidMatcher初期化テスト ---

class TestBidMatcherInit:

    def test_custom_config(self):
        config = {
            'filter': {
                'include_keywords': {'high_priority': ['AI']},
                'exclude_keywords': ['工事'],
                'notify_threshold': 5,
            }
        }
        m = BidMatcher(config=config)
        assert m.notify_threshold == 5
        assert m.exclude_keywords == ['工事']
        assert m.include_keywords == {'high_priority': ['AI']}

    def test_empty_config(self):
        m = BidMatcher(config={})
        assert m.notify_threshold == 2
        assert m.exclude_keywords == []
        assert m.include_keywords == {}

    def test_score_with_empty_keywords(self):
        m = BidMatcher(config={})
        bid = make_bid(title='何でもいい案件')
        score, matched = m.score_bid(bid)
        assert score == 0
