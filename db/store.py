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


# --- judgments (AI判定ログとフィードバック) ---

def insert_judgment(conn, bid_id, judgment, model=None):
    """LLM判定結果を判定ログテーブルに保存する。

    Args:
        bid_id: 案件ID
        judgment: BidJudgment dataclass instance
        model: 使用モデル名 (例: claude-haiku-4-5)

    Returns:
        int: 挿入された judgment の id
    """
    concerns_str = json.dumps(
        list(judgment.concerns) if judgment.concerns else [],
        ensure_ascii=False,
    )
    cur = conn.execute(
        """
        INSERT INTO judgments (
            bid_id, model, verdict, confidence, reason,
            estimated_effort, concerns, tool_calls
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            bid_id,
            model,
            judgment.verdict,
            judgment.confidence,
            judgment.reason,
            judgment.estimated_effort,
            concerns_str,
            judgment.tool_calls,
        ),
    )
    conn.commit()
    return cur.lastrowid


def record_judgment_outcome(conn, bid_id, outcome, note=None):
    """指定 bid_id の最新判定に outcome を記録する。

    outcome: applied / skipped / won / lost
    複数判定がある場合は最新 (judged_at DESC) のものに記録。

    Returns:
        int: 更新された行数 (該当判定がなければ 0)
    """
    valid_outcomes = ("applied", "skipped", "won", "lost")
    if outcome not in valid_outcomes:
        raise ValueError(
            f"Invalid outcome: {outcome}. Must be one of {valid_outcomes}"
        )

    row = conn.execute(
        "SELECT id FROM judgments WHERE bid_id = ? "
        "ORDER BY judged_at DESC, id DESC LIMIT 1",
        (bid_id,),
    ).fetchone()
    if not row:
        return 0

    conn.execute(
        """
        UPDATE judgments
        SET outcome = ?, outcome_at = ?, outcome_note = ?
        WHERE id = ?
        """,
        (outcome, datetime.now().isoformat(), note, row["id"]),
    )
    conn.commit()
    return 1


def get_judgments_by_bid(conn, bid_id):
    """指定 bid_id の判定履歴 (古い順) を取得する。"""
    return conn.execute(
        "SELECT * FROM judgments WHERE bid_id = ? ORDER BY judged_at ASC, id ASC",
        (bid_id,),
    ).fetchall()


def get_judgment_stats(conn):
    """判定ログの集計を返す。

    Returns:
        dict with:
            total: 全判定数
            by_verdict: {'apply': N, 'skip': N, 'uncertain': N}
            with_outcome: 結果フィードバックがある判定数
            agreement: dict of (verdict, outcome) -> count
            accuracy: applied vs apply, skipped vs skip の単純精度 (None if outcome 0件)
    """
    total = conn.execute("SELECT COUNT(*) FROM judgments").fetchone()[0]

    by_verdict_rows = conn.execute(
        "SELECT verdict, COUNT(*) FROM judgments GROUP BY verdict"
    ).fetchall()
    by_verdict = {row[0]: row[1] for row in by_verdict_rows}

    with_outcome = conn.execute(
        "SELECT COUNT(*) FROM judgments WHERE outcome IS NOT NULL"
    ).fetchone()[0]

    agreement_rows = conn.execute(
        """
        SELECT verdict, outcome, COUNT(*) as n
        FROM judgments
        WHERE outcome IS NOT NULL
        GROUP BY verdict, outcome
        """
    ).fetchall()
    agreement = {(row[0], row[1]): row[2] for row in agreement_rows}

    # 単純精度: apply→applied/won, skip→skipped を正解とみなす
    accuracy = None
    if with_outcome > 0:
        correct = 0
        for (verdict, outcome), n in agreement.items():
            if verdict == "apply" and outcome in ("applied", "won"):
                correct += n
            elif verdict == "skip" and outcome == "skipped":
                correct += n
        accuracy = correct / with_outcome

    return {
        "total": total,
        "by_verdict": by_verdict,
        "with_outcome": with_outcome,
        "agreement": agreement,
        "accuracy": accuracy,
    }
