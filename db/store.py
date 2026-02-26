"""DB操作（CRUD）"""

import hashlib
import json
import os
from datetime import datetime

from db.models import get_connection, init_db


def url_hash(url):
    """URLのSHA256ハッシュを生成する"""
    return hashlib.sha256(url.encode('utf-8')).hexdigest()


# --- municipalities ---

def insert_municipality(conn, municipality):
    """自治体を挿入する（既存なら更新）"""
    conn.execute("""
        INSERT INTO municipalities (code, name, prefecture, region, population,
                                    bid_page_url, news_page_url, page_type, active)
        VALUES (:code, :name, :prefecture, :region, :population,
                :bid_page_url, :news_page_url, :page_type, :active)
        ON CONFLICT(code) DO UPDATE SET
            name=excluded.name,
            prefecture=excluded.prefecture,
            region=excluded.region,
            population=excluded.population,
            bid_page_url=excluded.bid_page_url,
            news_page_url=excluded.news_page_url,
            page_type=excluded.page_type,
            active=excluded.active
    """, municipality)
    conn.commit()


def get_municipalities(conn, active_only=True):
    """自治体一覧を取得する"""
    if active_only:
        rows = conn.execute(
            "SELECT * FROM municipalities WHERE active = 1"
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM municipalities").fetchall()
    return rows


def get_municipality_by_code(conn, code):
    """自治体コードで1件取得する"""
    return conn.execute(
        "SELECT * FROM municipalities WHERE code = ?", (code,)
    ).fetchone()


def import_municipalities_from_json(conn, json_path=None):
    """municipalities.jsonからデータを読み込んでDBにインポートする"""
    if json_path is None:
        json_path = os.path.join(
            os.path.dirname(__file__), '..', 'data', 'municipalities.json'
        )
    with open(json_path, 'r', encoding='utf-8') as f:
        municipalities = json.load(f)

    count = 0
    for m in municipalities:
        data = {
            'code': m['code'],
            'name': m['name'],
            'prefecture': m['prefecture'],
            'region': m.get('region', '四国'),
            'population': m.get('population'),
            'bid_page_url': m.get('urls', {}).get('bid_page'),
            'news_page_url': m.get('urls', {}).get('news_page'),
            'page_type': m.get('page_type', 'unknown'),
            'active': 1 if m.get('active', True) else 0,
        }
        insert_municipality(conn, data)
        count += 1
    return count


def update_last_scraped(conn, code):
    """最終スクレイピング日時を更新する"""
    conn.execute(
        "UPDATE municipalities SET last_scraped_at = ? WHERE code = ?",
        (datetime.now().isoformat(), code)
    )
    conn.commit()


# --- bids ---

def insert_bid(conn, bid):
    """案件を挿入する。url_hashが重複していればスキップしてFalseを返す"""
    bid['url_hash'] = url_hash(bid['url'])
    try:
        conn.execute("""
            INSERT INTO bids (municipality_code, title, url, url_hash,
                              published_date, deadline, bid_type, budget_amount,
                              source, raw_text)
            VALUES (:municipality_code, :title, :url, :url_hash,
                    :published_date, :deadline, :bid_type, :budget_amount,
                    :source, :raw_text)
        """, bid)
        conn.commit()
        return True
    except Exception:
        return False


def get_bid_by_hash(conn, hash_value):
    """url_hashで案件を1件取得する"""
    return conn.execute(
        "SELECT * FROM bids WHERE url_hash = ?", (hash_value,)
    ).fetchone()


def get_bids_by_status(conn, status='new'):
    """ステータスで案件を取得する"""
    return conn.execute(
        "SELECT * FROM bids WHERE status = ?", (status,)
    ).fetchall()


def get_all_bids(conn):
    """全案件を取得する"""
    return conn.execute(
        "SELECT b.*, m.name as municipality_name, m.prefecture "
        "FROM bids b LEFT JOIN municipalities m ON b.municipality_code = m.code "
        "ORDER BY b.created_at DESC"
    ).fetchall()


def update_bid_score(conn, bid_id, score, matched_keywords):
    """案件のフィルタスコアとマッチキーワードを更新する"""
    conn.execute(
        "UPDATE bids SET filter_score = ?, matched_keywords = ? WHERE id = ?",
        (score, matched_keywords, bid_id)
    )
    conn.commit()


def update_bid_notified(conn, bid_id):
    """案件の通知日時を更新する"""
    conn.execute(
        "UPDATE bids SET notified_at = ?, status = 'notified' WHERE id = ?",
        (datetime.now().isoformat(), bid_id)
    )
    conn.commit()


# --- scrape_logs ---

def insert_scrape_log(conn, log):
    """スクレイピングログを挿入する"""
    conn.execute("""
        INSERT INTO scrape_logs (municipality_code, url, status_code,
                                 success, error_message, items_found, new_items)
        VALUES (:municipality_code, :url, :status_code,
                :success, :error_message, :items_found, :new_items)
    """, log)
    conn.commit()


def get_scrape_summary(conn, date=None):
    """スクレイピングサマリを取得する（日付指定なしなら本日分）"""
    if date is None:
        date = datetime.now().strftime('%Y-%m-%d')
    row = conn.execute("""
        SELECT
            COUNT(DISTINCT municipality_code) as total_municipalities,
            SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success_count,
            SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as failure_count,
            SUM(new_items) as total_new_items
        FROM scrape_logs
        WHERE DATE(scraped_at) = ?
    """, (date,)).fetchone()
    return row
