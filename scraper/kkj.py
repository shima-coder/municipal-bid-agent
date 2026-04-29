"""官公需ポータルスクレイパー"""

import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from db.store import insert_bid, insert_scrape_log
from scraper.base import BaseScraper

logger = logging.getLogger(__name__)

# 官公需ポータルサイト検索URL
KKJ_SEARCH_URL = 'https://www.kkj.go.jp/s/'
KKJ_BASE_URL = 'https://www.kkj.go.jp'

# 四国4県の都道府県コード (デフォルト)。config.yamlの scraper.kkj_prefectures で上書き可
SHIKOKU_PREFECTURES = {
    '36': '徳島県',
    '37': '香川県',
    '38': '愛媛県',
    '39': '高知県',
}

# 拡張用の参考: 中国・九州地方 (config.yaml に追記すれば対象化)
# '31': 鳥取, '32': 島根, '33': 岡山, '34': 広島, '35': 山口
# '40': 福岡, '41': 佐賀, '42': 長崎, '43': 熊本, '44': 大分, '45': 宮崎, '46': 鹿児島, '47': 沖縄


def parse_kkj_results(html, base_url=KKJ_BASE_URL, prefecture_names=None):
    """官公需ポータルの検索結果HTMLから案件情報を抽出する。

    Returns:
        list[dict]: 抽出された案件リスト。各要素は以下のキー:
            - title: 案件名
            - url: 案件URL（KKJポータル上の詳細ページ）
            - published_date: 公告日（YYYY-MM-DD or None）
            - organization: 機関名
            - raw_text: 周辺テキスト
    """
    soup = BeautifulSoup(html, 'lxml')
    items = []
    seen_urls = set()

    # 検索結果は<ol>リスト内の<li>要素
    result_list = soup.find('ol')
    if not result_list:
        # <ol>がない場合、<li>を直接探す
        result_items = soup.find_all('li')
    else:
        result_items = result_list.find_all('li', recursive=False)

    for li in result_items:
        # タイトルリンクを探す（/d/?D= パターン）
        title_link = None
        for a in li.find_all('a', href=True):
            href = a.get('href', '')
            if '/d/' in href and 'D=' in href:
                title_link = a
                break

        if not title_link:
            # フォールバック: li内の最初のリンク
            links = li.find_all('a', href=True)
            if links:
                title_link = links[0]

        if not title_link:
            continue

        # タイトルテキスト（ハイライト用spanを含む場合があるのでget_text）
        title = title_link.get_text(strip=True)
        if not title:
            continue

        # URL
        href = title_link.get('href', '')
        abs_url = urljoin(base_url, href)

        if abs_url in seen_urls:
            continue
        seen_urls.add(abs_url)

        # li全体のテキスト
        li_text = li.get_text(separator=' ', strip=True)

        # 公告日を抽出（「公告日またはデータ取得日: YYYY-MM-DD」パターン）
        published_date = None
        date_match = re.search(
            r'(?:公告日|データ取得日)[：:\s]*(\d{4}-\d{2}-\d{2})', li_text
        )
        if date_match:
            published_date = date_match.group(1)
        else:
            # YYYY-MM-DD形式の日付を探す
            date_match = re.search(r'(\d{4}-\d{2}-\d{2})', li_text)
            if date_match:
                published_date = date_match.group(1)

        # 機関名を抽出（都道府県名や市町村名）
        organization = ''
        # テキストの先頭付近にある機関名を探す
        # prefecture_names が None の時は SHIKOKU_PREFECTURES を使う (後方互換)
        target_prefs = prefecture_names if prefecture_names else SHIKOKU_PREFECTURES.values()
        for pref_name in target_prefs:
            if pref_name in li_text:
                # 都道府県名の後に続く市区町村名も取得
                org_match = re.search(
                    rf'({re.escape(pref_name)}[^\s,。、）)]*(?:市|町|村|区)?)',
                    li_text
                )
                if org_match:
                    organization = org_match.group(1)
                else:
                    organization = pref_name
                break

        items.append({
            'title': title,
            'url': abs_url,
            'published_date': published_date,
            'organization': organization,
            'raw_text': li_text[:500],  # 長すぎるテキストは切り詰め
        })

    return items


def extract_hit_count(html):
    """検索結果のヒット件数を抽出する。

    Returns:
        int: ヒット件数。取得できなければ0。
    """
    match = re.search(r'ヒット件数[：:\s]*(\d+)', html)
    if match:
        return int(match.group(1))
    return 0


class KKJScraper(BaseScraper):
    """官公需ポータル用スクレイパー"""

    def __init__(self, config=None):
        super().__init__(config)
        if config is None:
            from scraper.base import load_config
            config = load_config()
        self.config = config
        # 対象都道府県 (config優先、なければ SHIKOKU_PREFECTURES)
        scraper_cfg = config.get('scraper', {}) or {}
        kkj_prefs = scraper_cfg.get('kkj_prefectures')
        if kkj_prefs:
            # YAMLでは数値codeが文字列化されないことがあるので str() を保証
            self.prefectures = {str(k): v for k, v in kkj_prefs.items()}
        else:
            self.prefectures = dict(SHIKOKU_PREFECTURES)

    def _get_search_keywords(self):
        """config.yamlのinclude_keywordsからフラットなキーワードリストを取得する"""
        filter_config = self.config.get('filter', {})
        include = filter_config.get('include_keywords', {})
        keywords = []
        for priority in ('high_priority', 'medium_priority', 'low_priority'):
            keywords.extend(include.get(priority, []))
        return keywords

    def _build_search_params(self, keyword):
        """検索パラメータを構築する"""
        params = {
            'U': '0-all',
            'S': keyword,
            'pr': list(self.prefectures.keys()),
            'rc': '50',  # 1ページ最大50件
        }
        return params

    def search_keyword(self, conn, keyword):
        """1つのキーワードで官公需ポータルを検索し、新規案件をDBに保存する。

        Args:
            conn: SQLiteコネクション
            keyword: 検索キーワード

        Returns:
            dict: {'items_found': int, 'new_items': int}
        """
        params = self._build_search_params(keyword)

        # URLを構築（requestsのparamsではリスト型パラメータの扱いが特殊なため手動構築）
        param_parts = [f'U={params["U"]}', f'S={keyword}', f'rc={params["rc"]}']
        for pr in params['pr']:
            param_parts.append(f'pr={pr}')
        search_url = KKJ_SEARCH_URL + '?' + '&'.join(param_parts)

        html = self.fetch(search_url, conn=conn)
        if html is None:
            return {'items_found': 0, 'new_items': 0}

        hit_count = extract_hit_count(html)
        items = parse_kkj_results(html, prefecture_names=list(self.prefectures.values()))
        new_count = 0

        for item in items:
            # bid_typeはraw_textから推定
            from scraper.municipal import detect_bid_type
            bid_type = detect_bid_type(item['raw_text'])

            bid_data = {
                'municipality_code': '',  # 官公需ポータルでは自治体コード不明
                'title': item['title'],
                'url': item['url'],
                'published_date': item['published_date'],
                'deadline': None,
                'bid_type': bid_type,
                'budget_amount': None,
                'source': 'kkj_portal',
                'raw_text': item['raw_text'],
            }
            if insert_bid(conn, bid_data):
                new_count += 1

        # scrape_logを更新
        conn.execute(
            "UPDATE scrape_logs SET items_found = ?, new_items = ? "
            "WHERE id = (SELECT MAX(id) FROM scrape_logs WHERE url = ?)",
            (len(items), new_count, search_url)
        )
        conn.commit()

        logger.info(
            f"KKJ search '{keyword}': hit_count={hit_count}, "
            f"parsed={len(items)}, new={new_count}"
        )
        return {'items_found': len(items), 'new_items': new_count}

    def scrape_all(self, conn):
        """全キーワードで官公需ポータルを検索する。

        Args:
            conn: SQLiteコネクション

        Returns:
            dict: {'total_keywords': int, 'total_found': int, 'total_new': int}
        """
        keywords = self._get_search_keywords()
        total_found = 0
        total_new = 0

        for keyword in keywords:
            try:
                result = self.search_keyword(conn, keyword)
                total_found += result['items_found']
                total_new += result['new_items']
            except Exception as e:
                logger.error(f"Error searching KKJ for '{keyword}': {e}")

        logger.info(
            f"KKJ scraping complete: {len(keywords)} keywords, "
            f"found={total_found}, new={total_new}"
        )
        return {
            'total_keywords': len(keywords),
            'total_found': total_found,
            'total_new': total_new,
        }
