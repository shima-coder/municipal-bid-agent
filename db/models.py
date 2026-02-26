"""SQLiteテーブル定義・初期化"""

import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'bids.db')

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS municipalities (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    prefecture TEXT NOT NULL,
    region TEXT,
    population INTEGER,
    bid_page_url TEXT,
    news_page_url TEXT,
    page_type TEXT,
    active BOOLEAN DEFAULT 1,
    last_scraped_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bids (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    municipality_code TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    url_hash TEXT UNIQUE NOT NULL,
    published_date DATE,
    deadline DATE,
    bid_type TEXT DEFAULT 'unknown',
    budget_amount INTEGER,
    source TEXT DEFAULT 'municipal_hp',
    raw_text TEXT,
    filter_score INTEGER DEFAULT 0,
    matched_keywords TEXT,
    status TEXT DEFAULT 'new',
    notified_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS scrape_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    municipality_code TEXT,
    url TEXT,
    status_code INTEGER,
    success BOOLEAN,
    error_message TEXT,
    items_found INTEGER DEFAULT 0,
    new_items INTEGER DEFAULT 0,
    scraped_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


def get_connection(db_path=None):
    """SQLiteコネクションを取得する"""
    if db_path is None:
        db_path = DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path=None):
    """テーブルが存在しなければ自動作成する"""
    conn = get_connection(db_path)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()
