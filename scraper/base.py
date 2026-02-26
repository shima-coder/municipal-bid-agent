"""ベーススクレイパー（共通処理）"""

import logging
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import chardet
import requests
import yaml

from db.store import insert_scrape_log

logger = logging.getLogger(__name__)


def load_config(config_path='config.yaml'):
    """config.yamlを読み込む"""
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


class BaseScraper:
    """全スクレイパー共通の基底クラス"""

    def __init__(self, config=None):
        if config is None:
            config = load_config()
        scraper_config = config.get('scraper', {})

        self.user_agent = scraper_config.get('user_agent', 'MunicipalBidScraper/1.0')
        self.request_interval = scraper_config.get('request_interval', 3)
        self.timeout = scraper_config.get('timeout', 30)
        self.max_retries = scraper_config.get('max_retries', 3)

        self.session = requests.Session()
        self.session.headers.update({'User-Agent': self.user_agent})

        # ドメインごとの最終アクセス時刻
        self._last_access = {}
        # ドメインごとのrobots.txtキャッシュ
        self._robots_cache = {}

    def _wait_for_domain(self, url):
        """同一ドメインへのリクエスト間隔を制御する"""
        domain = urlparse(url).netloc
        now = time.time()
        last = self._last_access.get(domain, 0)
        wait = self.request_interval - (now - last)
        if wait > 0:
            logger.debug(f"Waiting {wait:.1f}s for {domain}")
            time.sleep(wait)
        self._last_access[domain] = time.time()

    def check_robots_txt(self, url):
        """robots.txtをチェックし、アクセス可能かどうかを返す"""
        parsed = urlparse(url)
        domain = parsed.netloc
        if domain in self._robots_cache:
            return self._robots_cache[domain].can_fetch(self.user_agent, url)

        robots_url = f"{parsed.scheme}://{domain}/robots.txt"
        rp = RobotFileParser()
        try:
            rp.set_url(robots_url)
            rp.read()
        except Exception:
            logger.debug(f"robots.txt not found or unreadable: {robots_url}")
            rp = _permissive_robot_parser()
        self._robots_cache[domain] = rp
        return rp.can_fetch(self.user_agent, url)

    def _decode_response(self, response):
        """レスポンスの文字コードを判定してテキストを返す"""
        # レスポンスヘッダのcharsetがある場合はそれを使う
        if response.encoding and response.encoding.lower() != 'iso-8859-1':
            return response.text

        # chardetで推定
        detected = chardet.detect(response.content)
        encoding = detected.get('encoding')
        if encoding:
            try:
                return response.content.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                pass

        # 最終フォールバック: utf-8
        return response.content.decode('utf-8', errors='replace')

    def fetch(self, url, conn=None, municipality_code=None):
        """URLからHTMLを取得する。リトライ・レート制限・robots.txt対応。

        Returns:
            str or None: 取得したHTMLテキスト。失敗時はNone。
        """
        # robots.txtチェック
        if not self.check_robots_txt(url):
            logger.warning(f"Blocked by robots.txt: {url}")
            if conn:
                insert_scrape_log(conn, {
                    'municipality_code': municipality_code,
                    'url': url,
                    'status_code': None,
                    'success': False,
                    'error_message': 'Blocked by robots.txt',
                    'items_found': 0,
                    'new_items': 0,
                })
            return None

        last_error = None
        for attempt in range(1, self.max_retries + 1):
            self._wait_for_domain(url)
            try:
                response = self.session.get(
                    url, timeout=self.timeout, verify=True
                )
                response.raise_for_status()
                text = self._decode_response(response)

                if conn:
                    insert_scrape_log(conn, {
                        'municipality_code': municipality_code,
                        'url': url,
                        'status_code': response.status_code,
                        'success': True,
                        'error_message': None,
                        'items_found': 0,
                        'new_items': 0,
                    })
                return text

            except requests.exceptions.SSLError as e:
                logger.warning(f"SSL error for {url}, retrying with verify=False: {e}")
                try:
                    self._wait_for_domain(url)
                    response = self.session.get(
                        url, timeout=self.timeout, verify=False
                    )
                    response.raise_for_status()
                    text = self._decode_response(response)

                    if conn:
                        insert_scrape_log(conn, {
                            'municipality_code': municipality_code,
                            'url': url,
                            'status_code': response.status_code,
                            'success': True,
                            'error_message': 'SSL verify disabled',
                            'items_found': 0,
                            'new_items': 0,
                        })
                    return text
                except Exception as e2:
                    last_error = str(e2)
                    logger.error(f"SSL fallback also failed for {url}: {e2}")

            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                last_error = str(e)
                backoff = 2 ** (attempt - 1)
                logger.warning(
                    f"Attempt {attempt}/{self.max_retries} failed for {url}: {e}. "
                    f"Retrying in {backoff}s..."
                )
                time.sleep(backoff)

            except requests.exceptions.HTTPError as e:
                status_code = e.response.status_code if e.response is not None else None
                last_error = str(e)
                if status_code and 500 <= status_code < 600:
                    backoff = 2 ** (attempt - 1)
                    logger.warning(
                        f"Server error {status_code} for {url}. "
                        f"Retry {attempt}/{self.max_retries} in {backoff}s..."
                    )
                    time.sleep(backoff)
                else:
                    # 4xx系はリトライしない
                    logger.error(f"HTTP {status_code} for {url}: {e}")
                    if conn:
                        insert_scrape_log(conn, {
                            'municipality_code': municipality_code,
                            'url': url,
                            'status_code': status_code,
                            'success': False,
                            'error_message': last_error,
                            'items_found': 0,
                            'new_items': 0,
                        })
                    return None

            except Exception as e:
                last_error = str(e)
                logger.error(f"Unexpected error for {url}: {e}")
                break

        # 全リトライ失敗
        logger.error(f"All {self.max_retries} retries failed for {url}")
        if conn:
            insert_scrape_log(conn, {
                'municipality_code': municipality_code,
                'url': url,
                'status_code': None,
                'success': False,
                'error_message': last_error,
                'items_found': 0,
                'new_items': 0,
            })
        return None


def _permissive_robot_parser():
    """robots.txtが取得できない場合に全許可するパーサーを返す"""
    rp = RobotFileParser()
    rp.parse([])
    return rp
