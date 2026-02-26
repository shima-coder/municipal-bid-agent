"""ベーススクレイパーのテスト"""

import sqlite3
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from db.models import init_db, get_connection
from db.store import insert_municipality, get_bid_by_hash, url_hash
from scraper.base import BaseScraper, load_config
from scraper.municipal import (
    MunicipalScraper,
    detect_bid_type,
    extract_published_date,
    parse_links_from_html,
)
from scraper.kkj import (
    KKJScraper,
    parse_kkj_results,
    extract_hit_count,
    KKJ_BASE_URL,
)


@pytest.fixture
def config():
    return {
        'scraper': {
            'user_agent': 'TestScraper/1.0',
            'request_interval': 0,  # テストでは待たない
            'timeout': 5,
            'max_retries': 2,
        }
    }


@pytest.fixture
def scraper(config):
    return BaseScraper(config=config)


@pytest.fixture
def db_conn(tmp_path):
    db_path = str(tmp_path / 'test.db')
    init_db(db_path)
    conn = get_connection(db_path)
    yield conn
    conn.close()


class TestBaseScraper:
    def test_init_default_config(self):
        """デフォルト設定でインスタンス生成できる"""
        with patch('scraper.base.load_config', return_value={'scraper': {}}):
            s = BaseScraper()
        assert s.user_agent == 'MunicipalBidScraper/1.0'
        assert s.request_interval == 3
        assert s.timeout == 30
        assert s.max_retries == 3

    def test_init_custom_config(self, scraper):
        """カスタム設定が反映される"""
        assert scraper.user_agent == 'TestScraper/1.0'
        assert scraper.request_interval == 0
        assert scraper.timeout == 5
        assert scraper.max_retries == 2

    def test_user_agent_header(self, scraper):
        """セッションにUser-Agentが設定されている"""
        assert scraper.session.headers['User-Agent'] == 'TestScraper/1.0'


class TestRateLimit:
    def test_wait_for_domain(self, config):
        """同一ドメインへのリクエスト間隔が制御される"""
        config['scraper']['request_interval'] = 0.5
        scraper = BaseScraper(config=config)
        url = 'https://example.com/page1'

        # 初回は待たない
        start = time.time()
        scraper._wait_for_domain(url)
        elapsed = time.time() - start
        assert elapsed < 0.1

        # 2回目は待つ
        start = time.time()
        scraper._wait_for_domain(url)
        elapsed = time.time() - start
        assert elapsed >= 0.4

    def test_different_domains_no_wait(self, scraper):
        """異なるドメインには待たない"""
        scraper._wait_for_domain('https://a.example.com/')
        start = time.time()
        scraper._wait_for_domain('https://b.example.com/')
        elapsed = time.time() - start
        assert elapsed < 0.1


class TestRobotsTxt:
    def test_robots_allowed(self, scraper):
        """robots.txtで許可されていればTrue"""
        with patch.object(scraper, '_robots_cache', {}):
            mock_rp = MagicMock()
            mock_rp.can_fetch.return_value = True
            with patch('scraper.base.RobotFileParser', return_value=mock_rp):
                assert scraper.check_robots_txt('https://example.com/page') is True

    def test_robots_disallowed(self, scraper):
        """robots.txtで禁止されていればFalse"""
        with patch.object(scraper, '_robots_cache', {}):
            mock_rp = MagicMock()
            mock_rp.can_fetch.return_value = False
            with patch('scraper.base.RobotFileParser', return_value=mock_rp):
                assert scraper.check_robots_txt('https://example.com/secret') is False

    def test_robots_cache(self, scraper):
        """同一ドメインのrobots.txtはキャッシュされる"""
        mock_rp = MagicMock()
        mock_rp.can_fetch.return_value = True
        scraper._robots_cache['example.com'] = mock_rp

        scraper.check_robots_txt('https://example.com/page1')
        scraper.check_robots_txt('https://example.com/page2')
        assert mock_rp.can_fetch.call_count == 2
        # RobotFileParserが再作成されていないことを確認
        assert scraper._robots_cache['example.com'] is mock_rp


class TestDecodeResponse:
    def test_utf8_from_header(self, scraper):
        """レスポンスヘッダのutf-8が使われる"""
        mock_resp = MagicMock()
        mock_resp.encoding = 'utf-8'
        mock_resp.text = 'テスト'
        assert scraper._decode_response(mock_resp) == 'テスト'

    def test_shift_jis_by_chardet(self, scraper):
        """chardetでShift_JISを検出する"""
        text = 'テスト文字列'
        content = text.encode('shift_jis')
        mock_resp = MagicMock()
        mock_resp.encoding = 'iso-8859-1'  # requestsのデフォルト
        mock_resp.content = content
        result = scraper._decode_response(mock_resp)
        assert 'テスト' in result

    def test_euc_jp_by_chardet(self, scraper):
        """chardetでEUC-JPを検出する"""
        text = '日本語テキスト'
        content = text.encode('euc-jp')
        mock_resp = MagicMock()
        mock_resp.encoding = 'iso-8859-1'
        mock_resp.content = content
        result = scraper._decode_response(mock_resp)
        assert '日本語' in result

    def test_fallback_to_utf8(self, scraper):
        """デコードできない場合はutf-8 replaceにフォールバック"""
        mock_resp = MagicMock()
        mock_resp.encoding = 'iso-8859-1'
        mock_resp.content = b'\xff\xfe invalid'
        import chardet as chardet_mod
        with patch.object(chardet_mod, 'detect', return_value={'encoding': None}):
            result = scraper._decode_response(mock_resp)
        assert isinstance(result, str)


class TestFetch:
    @patch.object(BaseScraper, 'check_robots_txt', return_value=True)
    def test_fetch_success(self, mock_robots, scraper):
        """正常にHTMLを取得する"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.encoding = 'utf-8'
        mock_resp.text = '<html>テスト</html>'
        mock_resp.raise_for_status = MagicMock()
        with patch.object(scraper.session, 'get', return_value=mock_resp):
            result = scraper.fetch('https://example.com/')
        assert result == '<html>テスト</html>'

    @patch.object(BaseScraper, 'check_robots_txt', return_value=False)
    def test_fetch_blocked_by_robots(self, mock_robots, scraper):
        """robots.txtでブロックされた場合はNoneを返す"""
        result = scraper.fetch('https://example.com/secret')
        assert result is None

    @patch.object(BaseScraper, 'check_robots_txt', return_value=False)
    def test_fetch_blocked_logs_to_db(self, mock_robots, scraper, db_conn):
        """robots.txtブロック時にDBにログが記録される"""
        scraper.fetch('https://example.com/secret', conn=db_conn, municipality_code='123456')
        logs = db_conn.execute("SELECT * FROM scrape_logs").fetchall()
        assert len(logs) == 1
        assert logs[0]['success'] == 0
        assert 'robots.txt' in logs[0]['error_message']

    @patch.object(BaseScraper, 'check_robots_txt', return_value=True)
    def test_fetch_success_logs_to_db(self, mock_robots, scraper, db_conn):
        """成功時にDBにログが記録される"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.encoding = 'utf-8'
        mock_resp.text = '<html></html>'
        mock_resp.raise_for_status = MagicMock()
        with patch.object(scraper.session, 'get', return_value=mock_resp):
            scraper.fetch('https://example.com/', conn=db_conn, municipality_code='123456')
        logs = db_conn.execute("SELECT * FROM scrape_logs").fetchall()
        assert len(logs) == 1
        assert logs[0]['success'] == 1
        assert logs[0]['status_code'] == 200

    @patch.object(BaseScraper, 'check_robots_txt', return_value=True)
    def test_fetch_retry_on_connection_error(self, mock_robots, scraper):
        """ConnectionErrorでリトライする"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.encoding = 'utf-8'
        mock_resp.text = '<html>ok</html>'
        mock_resp.raise_for_status = MagicMock()

        with patch.object(scraper.session, 'get',
                          side_effect=[requests.exceptions.ConnectionError('fail'), mock_resp]):
            result = scraper.fetch('https://example.com/')
        assert result == '<html>ok</html>'

    @patch.object(BaseScraper, 'check_robots_txt', return_value=True)
    def test_fetch_retry_on_timeout(self, mock_robots, scraper):
        """Timeoutでリトライする"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.encoding = 'utf-8'
        mock_resp.text = '<html>ok</html>'
        mock_resp.raise_for_status = MagicMock()

        with patch.object(scraper.session, 'get',
                          side_effect=[requests.exceptions.Timeout('timeout'), mock_resp]):
            result = scraper.fetch('https://example.com/')
        assert result == '<html>ok</html>'

    @patch.object(BaseScraper, 'check_robots_txt', return_value=True)
    def test_fetch_retry_on_500(self, mock_robots, scraper):
        """5xxエラーでリトライする"""
        error_resp = MagicMock()
        error_resp.status_code = 503
        http_error = requests.exceptions.HTTPError(response=error_resp)
        error_resp.raise_for_status = MagicMock(side_effect=http_error)

        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.encoding = 'utf-8'
        ok_resp.text = '<html>ok</html>'
        ok_resp.raise_for_status = MagicMock()

        with patch.object(scraper.session, 'get',
                          side_effect=[error_resp, ok_resp]):
            result = scraper.fetch('https://example.com/')
        assert result == '<html>ok</html>'

    @patch.object(BaseScraper, 'check_robots_txt', return_value=True)
    def test_fetch_no_retry_on_404(self, mock_robots, scraper):
        """404エラーではリトライしない"""
        error_resp = MagicMock()
        error_resp.status_code = 404
        http_error = requests.exceptions.HTTPError(response=error_resp)
        error_resp.raise_for_status = MagicMock(side_effect=http_error)

        with patch.object(scraper.session, 'get', return_value=error_resp):
            result = scraper.fetch('https://example.com/notfound')
        assert result is None

    @patch.object(BaseScraper, 'check_robots_txt', return_value=True)
    def test_fetch_ssl_error_fallback(self, mock_robots, scraper):
        """SSLエラー時にverify=Falseでフォールバックする"""
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.encoding = 'utf-8'
        ok_resp.text = '<html>ok</html>'
        ok_resp.raise_for_status = MagicMock()

        def side_effect(url, timeout, verify):
            if verify:
                raise requests.exceptions.SSLError('cert error')
            return ok_resp

        with patch.object(scraper.session, 'get', side_effect=side_effect):
            result = scraper.fetch('https://example.com/')
        assert result == '<html>ok</html>'

    @patch.object(BaseScraper, 'check_robots_txt', return_value=True)
    def test_fetch_all_retries_fail(self, mock_robots, scraper, db_conn):
        """全リトライ失敗時にNoneを返しログを記録する"""
        with patch.object(scraper.session, 'get',
                          side_effect=requests.exceptions.ConnectionError('fail')):
            result = scraper.fetch('https://example.com/', conn=db_conn, municipality_code='123456')
        assert result is None
        logs = db_conn.execute("SELECT * FROM scrape_logs").fetchall()
        assert len(logs) == 1
        assert logs[0]['success'] == 0


# ============================================================
# MunicipalScraper テスト
# ============================================================

class TestDetectBidType:
    def test_proposal(self):
        assert detect_bid_type('公募型プロポーザルの実施') == 'proposal'

    def test_proposal_kikaku(self):
        assert detect_bid_type('企画提案の募集について') == 'proposal'

    def test_proposal_kikaku_kyousou(self):
        assert detect_bid_type('企画競争入札のお知らせ') == 'proposal'

    def test_bid(self):
        assert detect_bid_type('一般競争入札の公告') == 'bid'

    def test_bid_shimei(self):
        assert detect_bid_type('指名競争入札結果') == 'bid'

    def test_negotiation(self):
        assert detect_bid_type('随意契約について') == 'negotiation'

    def test_negotiation_mitsumori(self):
        assert detect_bid_type('見積合わせの結果') == 'negotiation'

    def test_unknown(self):
        assert detect_bid_type('お知らせ') == 'unknown'

    def test_none(self):
        assert detect_bid_type(None) == 'unknown'

    def test_empty(self):
        assert detect_bid_type('') == 'unknown'


class TestExtractPublishedDate:
    def test_reiwa(self):
        assert extract_published_date('令和6年4月1日') == '2024-04-01'

    def test_western_slash(self):
        assert extract_published_date('掲載日: 2024/03/15') == '2024-03-15'

    def test_western_hyphen(self):
        assert extract_published_date('2024-12-25 公告') == '2024-12-25'

    def test_japanese_year(self):
        assert extract_published_date('2024年1月5日掲載') == '2024-01-05'

    def test_none(self):
        assert extract_published_date('日付なし') is None

    def test_empty(self):
        assert extract_published_date('') is None

    def test_null(self):
        assert extract_published_date(None) is None


class TestParseLinksFromHtml:
    """汎用パーサーのテスト"""

    def test_table_pattern(self):
        """<table>行からリンクを抽出する"""
        html = '''
        <html><body>
        <table>
          <tr>
            <td>2024/04/01</td>
            <td><a href="/bids/001.html">データ分析業務委託プロポーザル</a></td>
          </tr>
          <tr>
            <td>2024/04/02</td>
            <td><a href="/bids/002.html">道路清掃業務入札</a></td>
          </tr>
        </table>
        </body></html>
        '''
        items = parse_links_from_html(html, 'https://example.com/')
        assert len(items) == 2
        assert items[0]['title'] == 'データ分析業務委託プロポーザル'
        assert items[0]['url'] == 'https://example.com/bids/001.html'
        assert items[0]['published_date'] == '2024-04-01'
        assert items[1]['title'] == '道路清掃業務入札'
        assert items[1]['url'] == 'https://example.com/bids/002.html'

    def test_ul_li_pattern(self):
        """<ul><li>リストからリンクを抽出する"""
        html = '''
        <html><body>
        <ul>
          <li>令和6年3月10日 <a href="proposal.html">DX推進支援業務</a></li>
          <li>令和6年3月11日 <a href="bid.html">印刷業務</a></li>
        </ul>
        </body></html>
        '''
        items = parse_links_from_html(html, 'https://example.com/')
        assert len(items) == 2
        assert items[0]['title'] == 'DX推進支援業務'
        assert items[0]['url'] == 'https://example.com/proposal.html'
        assert items[0]['published_date'] == '2024-03-10'

    def test_dl_dt_dd_pattern(self):
        """<dl><dt><dd>からリンクを抽出する"""
        html = '''
        <html><body>
        <dl>
          <dt>2024年5月1日</dt>
          <dd><a href="detail.html">統計調査業務委託</a></dd>
        </dl>
        </body></html>
        '''
        items = parse_links_from_html(html, 'https://example.com/')
        assert len(items) == 1
        assert items[0]['title'] == '統計調査業務委託'
        assert items[0]['published_date'] == '2024-05-01'

    def test_div_a_pattern(self):
        """<div>直下の<a>タグからリンクを抽出する"""
        html = '''
        <html><body>
        <div>
          <a href="news.html">新着情報のお知らせ</a>
        </div>
        </body></html>
        '''
        items = parse_links_from_html(html, 'https://example.com/')
        assert len(items) == 1
        assert items[0]['title'] == '新着情報のお知らせ'
        assert items[0]['url'] == 'https://example.com/news.html'

    def test_pdf_link(self):
        """PDFリンクが抽出される"""
        html = '''
        <html><body>
        <ul>
          <li><a href="/files/bid001.pdf">入札公告（PDF）</a></li>
        </ul>
        </body></html>
        '''
        items = parse_links_from_html(html, 'https://example.com/')
        assert len(items) == 1
        assert items[0]['url'] == 'https://example.com/files/bid001.pdf'

    def test_relative_url_conversion(self):
        """相対URLが絶対URLに変換される"""
        html = '''
        <html><body>
        <ul><li><a href="../bids/detail.html">案件詳細</a></li></ul>
        </body></html>
        '''
        items = parse_links_from_html(html, 'https://example.com/pages/list.html')
        assert items[0]['url'] == 'https://example.com/bids/detail.html'

    def test_javascript_link_excluded(self):
        """javascript:リンクは除外される"""
        html = '''
        <html><body>
        <ul>
          <li><a href="javascript:void(0)">無視するリンク</a></li>
          <li><a href="real.html">有効なリンク</a></li>
        </ul>
        </body></html>
        '''
        items = parse_links_from_html(html, 'https://example.com/')
        assert len(items) == 1
        assert items[0]['title'] == '有効なリンク'

    def test_duplicate_url_excluded(self):
        """同一URLは重複除外される"""
        html = '''
        <html><body>
        <table><tr><td><a href="dup.html">案件A</a></td></tr></table>
        <ul><li><a href="dup.html">案件A</a></li></ul>
        </body></html>
        '''
        items = parse_links_from_html(html, 'https://example.com/')
        assert len(items) == 1

    def test_empty_title_excluded(self):
        """タイトルが空のリンクは除外される"""
        html = '''
        <html><body>
        <ul>
          <li><a href="empty.html">   </a></li>
          <li><a href="ok.html">有効</a></li>
        </ul>
        </body></html>
        '''
        items = parse_links_from_html(html, 'https://example.com/')
        assert len(items) == 1
        assert items[0]['title'] == '有効'


class TestMunicipalScraper:
    """MunicipalScraperの統合テスト"""

    @pytest.fixture
    def municipal_scraper(self, config):
        return MunicipalScraper(config=config)

    @pytest.fixture
    def db_with_municipality(self, db_conn):
        """テスト用自治体が登録されたDB"""
        insert_municipality(db_conn, {
            'code': '362018',
            'name': '松前町',
            'prefecture': '愛媛県',
            'region': '四国',
            'population': 28000,
            'bid_page_url': 'https://www.town.masaki.ehime.jp/bids/',
            'news_page_url': None,
            'page_type': 'html_list',
            'active': 1,
        })
        return db_conn

    @patch.object(BaseScraper, 'check_robots_txt', return_value=True)
    def test_scrape_municipality(self, mock_robots, municipal_scraper, db_with_municipality):
        """1自治体のスクレイピングで新規案件がDBに保存される"""
        mock_html = '''
        <html><body>
        <table>
          <tr>
            <td>2024/04/01</td>
            <td><a href="detail1.html">データ分析プロポーザル</a></td>
          </tr>
          <tr>
            <td>2024/04/02</td>
            <td><a href="detail2.html">一般競争入札公告</a></td>
          </tr>
        </table>
        </body></html>
        '''
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.encoding = 'utf-8'
        mock_resp.text = mock_html
        mock_resp.raise_for_status = MagicMock()

        with patch.object(municipal_scraper.session, 'get', return_value=mock_resp):
            m = db_with_municipality.execute(
                "SELECT * FROM municipalities WHERE code = '362018'"
            ).fetchone()
            result = municipal_scraper.scrape_municipality(db_with_municipality, m)

        assert result['items_found'] == 2
        assert result['new_items'] == 2

        # DBに案件が保存されている
        bids = db_with_municipality.execute("SELECT * FROM bids").fetchall()
        assert len(bids) == 2
        assert bids[0]['bid_type'] == 'proposal'
        assert bids[1]['bid_type'] == 'bid'
        assert bids[0]['municipality_code'] == '362018'

    @patch.object(BaseScraper, 'check_robots_txt', return_value=True)
    def test_scrape_dedup(self, mock_robots, municipal_scraper, db_with_municipality):
        """2回目のスクレイピングで重複案件は登録されない"""
        mock_html = '''
        <html><body>
        <ul><li><a href="detail.html">案件A</a></li></ul>
        </body></html>
        '''
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.encoding = 'utf-8'
        mock_resp.text = mock_html
        mock_resp.raise_for_status = MagicMock()

        with patch.object(municipal_scraper.session, 'get', return_value=mock_resp):
            m = db_with_municipality.execute(
                "SELECT * FROM municipalities WHERE code = '362018'"
            ).fetchone()
            result1 = municipal_scraper.scrape_municipality(db_with_municipality, m)
            result2 = municipal_scraper.scrape_municipality(db_with_municipality, m)

        assert result1['new_items'] == 1
        assert result2['new_items'] == 0
        bids = db_with_municipality.execute("SELECT * FROM bids").fetchall()
        assert len(bids) == 1

    @patch.object(BaseScraper, 'check_robots_txt', return_value=True)
    def test_scrape_municipality_no_urls(self, mock_robots, municipal_scraper, db_conn):
        """URLがない自治体はスキップされる"""
        insert_municipality(db_conn, {
            'code': '999999',
            'name': 'テスト村',
            'prefecture': '高知県',
            'region': '四国',
            'population': 1000,
            'bid_page_url': None,
            'news_page_url': None,
            'page_type': 'unknown',
            'active': 1,
        })
        m = db_conn.execute(
            "SELECT * FROM municipalities WHERE code = '999999'"
        ).fetchone()
        result = municipal_scraper.scrape_municipality(db_conn, m)
        assert result['items_found'] == 0
        assert result['new_items'] == 0

    @patch.object(BaseScraper, 'check_robots_txt', return_value=True)
    def test_scrape_all(self, mock_robots, municipal_scraper, db_with_municipality):
        """scrape_allが全active自治体を処理する"""
        mock_html = '<html><body><ul><li><a href="a.html">案件</a></li></ul></body></html>'
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.encoding = 'utf-8'
        mock_resp.text = mock_html
        mock_resp.raise_for_status = MagicMock()

        with patch.object(municipal_scraper.session, 'get', return_value=mock_resp):
            result = municipal_scraper.scrape_all(db_with_municipality)

        assert result['total_municipalities'] == 1
        assert result['success'] == 1
        assert result['failed'] == 0

    @patch.object(BaseScraper, 'check_robots_txt', return_value=True)
    def test_scrape_all_target_code(self, mock_robots, municipal_scraper, db_with_municipality):
        """target_code指定で特定自治体のみスクレイピングする"""
        mock_html = '<html><body><ul><li><a href="b.html">案件B</a></li></ul></body></html>'
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.encoding = 'utf-8'
        mock_resp.text = mock_html
        mock_resp.raise_for_status = MagicMock()

        with patch.object(municipal_scraper.session, 'get', return_value=mock_resp):
            result = municipal_scraper.scrape_all(db_with_municipality, target_code='362018')

        assert result['total_municipalities'] == 1
        assert result['success'] == 1

    def test_scrape_all_invalid_code(self, municipal_scraper, db_with_municipality):
        """存在しないコード指定で0件結果を返す"""
        result = municipal_scraper.scrape_all(db_with_municipality, target_code='000000')
        assert result['total_municipalities'] == 0


# ============================================================
# KKJScraper テスト
# ============================================================

class TestExtractHitCount:
    def test_normal(self):
        assert extract_hit_count('ヒット件数：14 件') == 14

    def test_zero(self):
        assert extract_hit_count('ヒット件数：0 件') == 0

    def test_large_number(self):
        assert extract_hit_count('ヒット件数：1234 件') == 1234

    def test_no_match(self):
        assert extract_hit_count('<html>検索結果なし</html>') == 0

    def test_colon_variation(self):
        assert extract_hit_count('ヒット件数: 5 件') == 5


class TestParseKkjResults:
    """官公需ポータル検索結果パーサーのテスト"""

    def test_basic_result(self):
        """基本的な検索結果を抽出できる"""
        html = '''
        <html><body>
        <ol>
          <li>
            <a href="/d/?D=abc123&L=ja">データ分析業務委託</a>
            香川県
            公告日またはデータ取得日: 2026-02-16
          </li>
        </ol>
        </body></html>
        '''
        items = parse_kkj_results(html)
        assert len(items) == 1
        assert items[0]['title'] == 'データ分析業務委託'
        assert items[0]['url'] == 'https://www.kkj.go.jp/d/?D=abc123&L=ja'
        assert items[0]['published_date'] == '2026-02-16'
        assert items[0]['organization'] == '香川県'

    def test_multiple_results(self):
        """複数の検索結果を抽出できる"""
        html = '''
        <html><body>
        <ol>
          <li>
            <a href="/d/?D=item1&L=ja">DX推進支援業務</a>
            愛媛県
            公告日またはデータ取得日: 2026-01-15
          </li>
          <li>
            <a href="/d/?D=item2&L=ja">統計調査委託</a>
            高知県
            公告日またはデータ取得日: 2026-01-10
          </li>
        </ol>
        </body></html>
        '''
        items = parse_kkj_results(html)
        assert len(items) == 2
        assert items[0]['title'] == 'DX推進支援業務'
        assert items[1]['title'] == '統計調査委託'

    def test_highlighted_title(self):
        """ハイライト用spanがあってもタイトルが正しく取得される"""
        html = '''
        <html><body>
        <ol>
          <li>
            <a href="/d/?D=xyz&L=ja"><span class="AtrekHighlight">データ分析</span>業務委託</a>
            徳島県
            公告日またはデータ取得日: 2026-02-01
          </li>
        </ol>
        </body></html>
        '''
        items = parse_kkj_results(html)
        assert len(items) == 1
        assert items[0]['title'] == 'データ分析業務委託'

    def test_no_results(self):
        """検索結果0件の場合は空リストを返す"""
        html = '<html><body><p>検索結果がありません</p></body></html>'
        items = parse_kkj_results(html)
        assert items == []

    def test_duplicate_url_excluded(self):
        """同一URLは重複除外される"""
        html = '''
        <html><body>
        <ol>
          <li><a href="/d/?D=same&L=ja">案件A</a></li>
          <li><a href="/d/?D=same&L=ja">案件A（再掲）</a></li>
        </ol>
        </body></html>
        '''
        items = parse_kkj_results(html)
        assert len(items) == 1

    def test_empty_title_excluded(self):
        """タイトルが空のリンクは除外される"""
        html = '''
        <html><body>
        <ol>
          <li><a href="/d/?D=empty&L=ja">   </a></li>
          <li><a href="/d/?D=valid&L=ja">有効な案件</a></li>
        </ol>
        </body></html>
        '''
        items = parse_kkj_results(html)
        assert len(items) == 1
        assert items[0]['title'] == '有効な案件'

    def test_date_extraction(self):
        """公告日が正しく抽出される"""
        html = '''
        <html><body>
        <ol>
          <li>
            <a href="/d/?D=d1&L=ja">案件1</a>
            公告日またはデータ取得日: 2025-12-25
          </li>
        </ol>
        </body></html>
        '''
        items = parse_kkj_results(html)
        assert items[0]['published_date'] == '2025-12-25'

    def test_no_date(self):
        """日付がない場合はNone"""
        html = '''
        <html><body>
        <ol>
          <li><a href="/d/?D=nodate&L=ja">日付なし案件</a></li>
        </ol>
        </body></html>
        '''
        items = parse_kkj_results(html)
        assert items[0]['published_date'] is None

    def test_municipality_organization(self):
        """市町村名が機関名として抽出される"""
        html = '''
        <html><body>
        <ol>
          <li>
            <a href="/d/?D=muni&L=ja">案件</a>
            徳島県小松島市 のデータ
            公告日またはデータ取得日: 2026-01-01
          </li>
        </ol>
        </body></html>
        '''
        items = parse_kkj_results(html)
        assert '徳島県' in items[0]['organization']

    def test_raw_text_truncated(self):
        """raw_textが500文字に切り詰められる"""
        long_text = 'あ' * 1000
        html = f'''
        <html><body>
        <ol>
          <li>
            <a href="/d/?D=long&L=ja">案件</a>
            {long_text}
          </li>
        </ol>
        </body></html>
        '''
        items = parse_kkj_results(html)
        assert len(items[0]['raw_text']) <= 500


class TestKKJScraper:
    """KKJScraperの統合テスト"""

    @pytest.fixture
    def kkj_config(self):
        return {
            'scraper': {
                'user_agent': 'TestScraper/1.0',
                'request_interval': 0,
                'timeout': 5,
                'max_retries': 2,
            },
            'filter': {
                'include_keywords': {
                    'high_priority': ['データ分析', 'ダッシュボード'],
                    'medium_priority': ['統計調査'],
                    'low_priority': ['調査業務'],
                },
            },
        }

    @pytest.fixture
    def kkj_scraper(self, kkj_config):
        return KKJScraper(config=kkj_config)

    def test_get_search_keywords(self, kkj_scraper):
        """config.yamlのキーワードがフラットなリストで取得される"""
        keywords = kkj_scraper._get_search_keywords()
        assert 'データ分析' in keywords
        assert 'ダッシュボード' in keywords
        assert '統計調査' in keywords
        assert '調査業務' in keywords
        assert len(keywords) == 4

    def test_build_search_params(self, kkj_scraper):
        """検索パラメータが正しく構築される"""
        params = kkj_scraper._build_search_params('データ分析')
        assert params['S'] == 'データ分析'
        assert params['U'] == '0-all'
        assert params['rc'] == '50'
        assert '36' in params['pr']
        assert '37' in params['pr']
        assert '38' in params['pr']
        assert '39' in params['pr']

    @patch.object(BaseScraper, 'check_robots_txt', return_value=True)
    def test_search_keyword(self, mock_robots, kkj_scraper, db_conn):
        """1キーワードの検索で新規案件がDBに保存される"""
        mock_html = '''
        <html><body>
        ヒット件数：2 件
        <ol>
          <li>
            <a href="/d/?D=abc&L=ja">データ分析プロポーザル</a>
            香川県
            公告日またはデータ取得日: 2026-02-16
          </li>
          <li>
            <a href="/d/?D=def&L=ja">一般競争入札データ分析</a>
            愛媛県
            公告日またはデータ取得日: 2026-02-10
          </li>
        </ol>
        </body></html>
        '''
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.encoding = 'utf-8'
        mock_resp.text = mock_html
        mock_resp.raise_for_status = MagicMock()

        with patch.object(kkj_scraper.session, 'get', return_value=mock_resp):
            result = kkj_scraper.search_keyword(db_conn, 'データ分析')

        assert result['items_found'] == 2
        assert result['new_items'] == 2

        bids = db_conn.execute("SELECT * FROM bids").fetchall()
        assert len(bids) == 2
        assert bids[0]['source'] == 'kkj_portal'
        assert bids[0]['bid_type'] == 'proposal'
        assert bids[1]['bid_type'] == 'bid'

    @patch.object(BaseScraper, 'check_robots_txt', return_value=True)
    def test_search_keyword_dedup(self, mock_robots, kkj_scraper, db_conn):
        """2回目の検索で重複案件は登録されない"""
        mock_html = '''
        <html><body>
        <ol>
          <li><a href="/d/?D=dup&L=ja">重複案件</a></li>
        </ol>
        </body></html>
        '''
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.encoding = 'utf-8'
        mock_resp.text = mock_html
        mock_resp.raise_for_status = MagicMock()

        with patch.object(kkj_scraper.session, 'get', return_value=mock_resp):
            r1 = kkj_scraper.search_keyword(db_conn, 'テスト')
            r2 = kkj_scraper.search_keyword(db_conn, 'テスト')

        assert r1['new_items'] == 1
        assert r2['new_items'] == 0
        bids = db_conn.execute("SELECT * FROM bids").fetchall()
        assert len(bids) == 1

    @patch.object(BaseScraper, 'check_robots_txt', return_value=True)
    def test_search_keyword_fetch_failure(self, mock_robots, kkj_scraper, db_conn):
        """fetch失敗時は0件を返す"""
        with patch.object(kkj_scraper.session, 'get',
                          side_effect=requests.exceptions.ConnectionError('fail')):
            result = kkj_scraper.search_keyword(db_conn, 'テスト')

        assert result['items_found'] == 0
        assert result['new_items'] == 0

    @patch.object(BaseScraper, 'check_robots_txt', return_value=True)
    def test_scrape_all(self, mock_robots, kkj_scraper, db_conn):
        """scrape_allが全キーワードで検索する"""
        call_count = 0

        def fake_get(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.encoding = 'utf-8'
            mock_resp.text = f'''
            <html><body>
            <ol>
              <li><a href="/d/?D=item{call_count}&L=ja">案件{call_count}</a></li>
            </ol>
            </body></html>
            '''
            mock_resp.raise_for_status = MagicMock()
            return mock_resp

        with patch.object(kkj_scraper.session, 'get', side_effect=fake_get):
            result = kkj_scraper.scrape_all(db_conn)

        assert result['total_keywords'] == 4
        assert result['total_found'] == 4
        assert result['total_new'] == 4
