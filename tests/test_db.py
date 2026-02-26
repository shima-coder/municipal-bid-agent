"""DBの基本CRUD動作テスト"""

import os
import tempfile
import pytest

from db.models import init_db, get_connection
from db.store import (
    insert_municipality, get_municipalities, get_municipality_by_code,
    update_last_scraped,
    insert_bid, get_bid_by_hash, get_bids_by_status, get_all_bids,
    update_bid_score, update_bid_notified,
    insert_scrape_log, get_scrape_summary,
    url_hash,
)


@pytest.fixture
def db_conn():
    """テスト用の一時DBを作成し、コネクションを返す"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, 'test.db')
        init_db(db_path)
        conn = get_connection(db_path)
        yield conn
        conn.close()


def sample_municipality():
    return {
        'code': '362018',
        'name': '松前町',
        'prefecture': '愛媛県',
        'region': '四国',
        'population': 28000,
        'bid_page_url': 'https://www.town.masaki.ehime.jp/bid/',
        'news_page_url': 'https://www.town.masaki.ehime.jp/news/',
        'page_type': 'html_list',
        'active': True,
    }


def sample_bid():
    return {
        'municipality_code': '362018',
        'title': 'データ分析業務委託',
        'url': 'https://www.town.masaki.ehime.jp/bid/001.html',
        'published_date': '2026-02-24',
        'deadline': '2026-03-15',
        'bid_type': 'proposal',
        'budget_amount': 3000000,
        'source': 'municipal_hp',
        'raw_text': 'データ分析業務委託に係るプロポーザル',
    }


# --- municipalities ---

class TestMunicipalities:
    def test_insert_and_get(self, db_conn):
        m = sample_municipality()
        insert_municipality(db_conn, m)
        rows = get_municipalities(db_conn)
        assert len(rows) == 1
        assert rows[0]['name'] == '松前町'
        assert rows[0]['prefecture'] == '愛媛県'

    def test_get_by_code(self, db_conn):
        m = sample_municipality()
        insert_municipality(db_conn, m)
        row = get_municipality_by_code(db_conn, '362018')
        assert row is not None
        assert row['code'] == '362018'

    def test_get_by_code_not_found(self, db_conn):
        row = get_municipality_by_code(db_conn, '999999')
        assert row is None

    def test_active_filter(self, db_conn):
        m = sample_municipality()
        insert_municipality(db_conn, m)

        m2 = sample_municipality()
        m2['code'] = '362019'
        m2['name'] = '砥部町'
        m2['active'] = False
        insert_municipality(db_conn, m2)

        active = get_municipalities(db_conn, active_only=True)
        assert len(active) == 1
        assert active[0]['code'] == '362018'

        all_rows = get_municipalities(db_conn, active_only=False)
        assert len(all_rows) == 2

    def test_upsert(self, db_conn):
        m = sample_municipality()
        insert_municipality(db_conn, m)
        m['population'] = 30000
        insert_municipality(db_conn, m)
        row = get_municipality_by_code(db_conn, '362018')
        assert row['population'] == 30000

    def test_update_last_scraped(self, db_conn):
        m = sample_municipality()
        insert_municipality(db_conn, m)
        update_last_scraped(db_conn, '362018')
        row = get_municipality_by_code(db_conn, '362018')
        assert row['last_scraped_at'] is not None


# --- bids ---

class TestBids:
    def test_insert_and_get(self, db_conn):
        insert_municipality(db_conn, sample_municipality())
        bid = sample_bid()
        result = insert_bid(db_conn, bid)
        assert result is True
        h = url_hash(bid['url'])
        row = get_bid_by_hash(db_conn, h)
        assert row is not None
        assert row['title'] == 'データ分析業務委託'

    def test_duplicate_url_rejected(self, db_conn):
        insert_municipality(db_conn, sample_municipality())
        bid = sample_bid()
        assert insert_bid(db_conn, bid) is True
        assert insert_bid(db_conn, bid) is False

    def test_get_by_status(self, db_conn):
        insert_municipality(db_conn, sample_municipality())
        insert_bid(db_conn, sample_bid())
        rows = get_bids_by_status(db_conn, 'new')
        assert len(rows) == 1

    def test_get_all_bids_with_join(self, db_conn):
        insert_municipality(db_conn, sample_municipality())
        insert_bid(db_conn, sample_bid())
        rows = get_all_bids(db_conn)
        assert len(rows) == 1
        assert rows[0]['municipality_name'] == '松前町'
        assert rows[0]['prefecture'] == '愛媛県'

    def test_update_score(self, db_conn):
        insert_municipality(db_conn, sample_municipality())
        insert_bid(db_conn, sample_bid())
        rows = get_bids_by_status(db_conn, 'new')
        bid_id = rows[0]['id']
        update_bid_score(db_conn, bid_id, 5, 'データ分析,プロポーザル')
        row = get_bid_by_hash(db_conn, url_hash(sample_bid()['url']))
        assert row['filter_score'] == 5
        assert row['matched_keywords'] == 'データ分析,プロポーザル'

    def test_update_notified(self, db_conn):
        insert_municipality(db_conn, sample_municipality())
        insert_bid(db_conn, sample_bid())
        rows = get_bids_by_status(db_conn, 'new')
        bid_id = rows[0]['id']
        update_bid_notified(db_conn, bid_id)
        row = get_bid_by_hash(db_conn, url_hash(sample_bid()['url']))
        assert row['status'] == 'notified'
        assert row['notified_at'] is not None


# --- scrape_logs ---

class TestScrapeLogs:
    def test_insert_and_summary(self, db_conn):
        log = {
            'municipality_code': '362018',
            'url': 'https://www.town.masaki.ehime.jp/bid/',
            'status_code': 200,
            'success': True,
            'error_message': None,
            'items_found': 10,
            'new_items': 3,
        }
        insert_scrape_log(db_conn, log)
        summary = get_scrape_summary(db_conn)
        assert summary['total_municipalities'] == 1
        assert summary['success_count'] == 1
        assert summary['failure_count'] == 0
        assert summary['total_new_items'] == 3

    def test_failure_log(self, db_conn):
        log = {
            'municipality_code': '362018',
            'url': 'https://www.town.masaki.ehime.jp/bid/',
            'status_code': 500,
            'success': False,
            'error_message': 'Internal Server Error',
            'items_found': 0,
            'new_items': 0,
        }
        insert_scrape_log(db_conn, log)
        summary = get_scrape_summary(db_conn)
        assert summary['failure_count'] == 1


# --- url_hash ---

class TestUrlHash:
    def test_deterministic(self):
        u = 'https://example.com/bid/001'
        assert url_hash(u) == url_hash(u)

    def test_different_urls(self):
        assert url_hash('https://a.com') != url_hash('https://b.com')
