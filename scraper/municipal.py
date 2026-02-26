"""自治体HP用スクレイパー"""

import hashlib
import logging
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from db.store import (
    get_municipalities,
    insert_bid,
    insert_scrape_log,
    update_last_scraped,
    url_hash,
)
from scraper.base import BaseScraper

logger = logging.getLogger(__name__)


def detect_bid_type(text):
    """テキストからbid_typeを推定する"""
    if not text:
        return 'unknown'
    if re.search(r'プロポーザル|企画提案|企画競争', text):
        return 'proposal'
    if re.search(r'入札|競争入札|一般競争|指名競争', text):
        return 'bid'
    if re.search(r'随意契約|見積合[わせ]せ', text):
        return 'negotiation'
    return 'unknown'


def extract_published_date(text):
    """テキストから公告日（YYYY-MM-DD）を抽出する。見つからなければNone。"""
    if not text:
        return None
    # 令和X年M月D日
    m = re.search(r'令和(\d{1,2})年(\d{1,2})月(\d{1,2})日', text)
    if m:
        year = 2018 + int(m.group(1))
        return f"{year}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    # YYYY年M月D日
    m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    # YYYY/MM/DD or YYYY-MM-DD
    m = re.search(r'(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})', text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def parse_links_from_html(html, base_url):
    """HTMLから案件リンク情報を抽出する汎用パーサー。

    Returns:
        list[dict]: 抽出された案件リスト。各要素は以下のキー:
            - title: 案件名
            - url: 絶対URL
            - raw_text: 周辺テキスト
            - published_date: 公告日（YYYY-MM-DD or None）
    """
    soup = BeautifulSoup(html, 'lxml')
    items = []
    seen_urls = set()

    def _add_item(title, href, context_text=''):
        """リンクアイテムを追加（重複URL除外）"""
        if not href or not title:
            return
        title = title.strip()
        if not title:
            return
        abs_url = urljoin(base_url, href)
        # 同一ページ内アンカーやjavascriptは除外
        if abs_url.startswith('javascript:') or abs_url == base_url:
            return
        if abs_url in seen_urls:
            return
        seen_urls.add(abs_url)
        raw_text = ' '.join((title + ' ' + context_text).split())
        items.append({
            'title': title,
            'url': abs_url,
            'raw_text': raw_text,
            'published_date': extract_published_date(context_text),
        })

    # パターン1: <table> 内の行
    for table in soup.find_all('table'):
        for row in table.find_all('tr'):
            links = row.find_all('a', href=True)
            row_text = row.get_text(separator=' ', strip=True)
            for link in links:
                _add_item(link.get_text(strip=True), link['href'], row_text)

    # パターン2: <ul><li> リスト
    for ul in soup.find_all('ul'):
        for li in ul.find_all('li', recursive=False):
            links = li.find_all('a', href=True)
            li_text = li.get_text(separator=' ', strip=True)
            for link in links:
                _add_item(link.get_text(strip=True), link['href'], li_text)

    # パターン3: <dl><dt><dd> 定義リスト
    for dl in soup.find_all('dl'):
        dts = dl.find_all('dt')
        for dt in dts:
            dd = dt.find_next_sibling('dd')
            context_text = dt.get_text(separator=' ', strip=True)
            if dd:
                context_text += ' ' + dd.get_text(separator=' ', strip=True)
            # dtかddからリンクを探す
            links = dt.find_all('a', href=True)
            if dd:
                links.extend(dd.find_all('a', href=True))
            for link in links:
                _add_item(link.get_text(strip=True), link['href'], context_text)

    # パターン4: <div> 内の <a> タグ（上記に含まれなかったもの）
    for div in soup.find_all('div'):
        for link in div.find_all('a', href=True, recursive=False):
            div_text = div.get_text(separator=' ', strip=True)
            _add_item(link.get_text(strip=True), link['href'], div_text)

    return items


class MunicipalScraper(BaseScraper):
    """自治体HP用スクレイパー"""

    def scrape_municipality(self, conn, municipality):
        """1つの自治体をスクレイピングし、新規案件をDBに保存する。

        Args:
            conn: SQLiteコネクション
            municipality: sqlite3.Row (municipalitiesテーブルの行)

        Returns:
            dict: {'items_found': int, 'new_items': int}
        """
        code = municipality['code']
        name = municipality['name']
        total_found = 0
        total_new = 0

        urls_to_scrape = []
        if municipality['bid_page_url']:
            urls_to_scrape.append(municipality['bid_page_url'])
        if municipality['news_page_url']:
            urls_to_scrape.append(municipality['news_page_url'])

        if not urls_to_scrape:
            logger.info(f"No URLs to scrape for {name} ({code})")
            return {'items_found': 0, 'new_items': 0}

        for page_url in urls_to_scrape:
            html = self.fetch(page_url, conn=conn, municipality_code=code)
            if html is None:
                continue

            items = parse_links_from_html(html, page_url)
            page_new = 0

            for item in items:
                bid_type = detect_bid_type(item['raw_text'])
                bid_data = {
                    'municipality_code': code,
                    'title': item['title'],
                    'url': item['url'],
                    'published_date': item['published_date'],
                    'deadline': None,
                    'bid_type': bid_type,
                    'budget_amount': None,
                    'source': 'municipal_hp',
                    'raw_text': item['raw_text'],
                }
                if insert_bid(conn, bid_data):
                    page_new += 1

            total_found += len(items)
            total_new += page_new

            # scrape_logのitems_found/new_itemsを更新
            # fetchで既にログが記録されているので、最新のログを更新する
            conn.execute(
                "UPDATE scrape_logs SET items_found = ?, new_items = ? "
                "WHERE id = (SELECT MAX(id) FROM scrape_logs WHERE url = ?)",
                (len(items), page_new, page_url)
            )
            conn.commit()

        update_last_scraped(conn, code)

        logger.info(
            f"Scraped {name} ({code}): "
            f"found={total_found}, new={total_new}"
        )
        return {'items_found': total_found, 'new_items': total_new}

    def scrape_all(self, conn, target_code=None):
        """全active自治体（またはtarget_code指定の1件）をスクレイピングする。

        Args:
            conn: SQLiteコネクション
            target_code: 特定の自治体コード（省略時は全active）

        Returns:
            dict: {'total_municipalities': int, 'success': int, 'failed': int,
                   'total_found': int, 'total_new': int}
        """
        if target_code:
            from db.store import get_municipality_by_code
            m = get_municipality_by_code(conn, target_code)
            if m is None:
                logger.error(f"Municipality not found: {target_code}")
                return {
                    'total_municipalities': 0, 'success': 0, 'failed': 0,
                    'total_found': 0, 'total_new': 0,
                }
            municipalities = [m]
        else:
            municipalities = get_municipalities(conn, active_only=True)

        total_municipalities = len(municipalities)
        success_count = 0
        failed_count = 0
        total_found = 0
        total_new = 0

        for m in municipalities:
            try:
                result = self.scrape_municipality(conn, m)
                total_found += result['items_found']
                total_new += result['new_items']
                success_count += 1
            except Exception as e:
                logger.error(
                    f"Error scraping {m['name']} ({m['code']}): {e}"
                )
                failed_count += 1

        logger.info(
            f"Scraping complete: {success_count}/{total_municipalities} success, "
            f"found={total_found}, new={total_new}"
        )
        return {
            'total_municipalities': total_municipalities,
            'success': success_count,
            'failed': failed_count,
            'total_found': total_found,
            'total_new': total_new,
        }
