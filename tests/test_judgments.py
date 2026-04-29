"""判定ログ (judgments) と outcome フィードバックのテスト"""

import json
import os
import tempfile

import pytest

from db.models import init_db, get_connection
from db.store import (
    insert_bid,
    insert_judgment,
    insert_municipality,
    record_judgment_outcome,
    get_judgments_by_bid,
    get_judgment_stats,
)
from judge.llm import BidJudgment


@pytest.fixture
def db_conn():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, 'test.db')
        init_db(db_path)
        conn = get_connection(db_path)
        yield conn
        conn.close()


@pytest.fixture
def seeded_bid(db_conn):
    """1自治体 + 1案件を挿入し、bid_idを返す"""
    insert_municipality(db_conn, {
        'code': '362018', 'name': '松前町', 'prefecture': '愛媛県',
        'region': '四国', 'population': 28000,
        'bid_page_url': 'https://example.jp/bid', 'news_page_url': None,
        'page_type': 'html_list', 'active': 1,
    })
    bid = {
        'municipality_code': '362018',
        'title': 'データ可視化基盤構築',
        'url': 'https://example.jp/bid/1',
        'published_date': '2026-04-20', 'deadline': None,
        'bid_type': 'proposal', 'budget_amount': None,
        'source': 'municipal_hp',
        'raw_text': 'データ可視化',
    }
    insert_bid(db_conn, bid)
    row = db_conn.execute("SELECT id FROM bids LIMIT 1").fetchone()
    return row['id']


class TestInsertJudgment:
    def test_basic_insert(self, db_conn, seeded_bid):
        j = BidJudgment(
            verdict="apply", confidence=80, reason="領域一致",
            estimated_effort="1人月", concerns=["短納期"], tool_calls=2,
        )
        jid = insert_judgment(db_conn, seeded_bid, j, model="claude-haiku-4-5")
        assert jid is not None

        row = db_conn.execute(
            "SELECT * FROM judgments WHERE id = ?", (jid,)
        ).fetchone()
        assert row['bid_id'] == seeded_bid
        assert row['verdict'] == "apply"
        assert row['confidence'] == 80
        assert row['model'] == "claude-haiku-4-5"
        assert row['tool_calls'] == 2
        assert json.loads(row['concerns']) == ["短納期"]
        assert row['outcome'] is None

    def test_empty_concerns_serialized(self, db_conn, seeded_bid):
        j = BidJudgment(verdict="skip", confidence=60, reason="x", concerns=[])
        jid = insert_judgment(db_conn, seeded_bid, j)
        row = db_conn.execute(
            "SELECT concerns FROM judgments WHERE id = ?", (jid,)
        ).fetchone()
        assert json.loads(row['concerns']) == []

    def test_multiple_judgments_per_bid(self, db_conn, seeded_bid):
        for v in ("uncertain", "apply"):
            insert_judgment(
                db_conn, seeded_bid,
                BidJudgment(verdict=v, confidence=50, reason="x"),
            )
        history = get_judgments_by_bid(db_conn, seeded_bid)
        assert len(history) == 2
        assert history[0]['verdict'] == "uncertain"
        assert history[1]['verdict'] == "apply"


class TestRecordOutcome:
    def test_record_applied(self, db_conn, seeded_bid):
        insert_judgment(
            db_conn, seeded_bid,
            BidJudgment(verdict="apply", confidence=80, reason="ok"),
        )
        n = record_judgment_outcome(db_conn, seeded_bid, "applied", "2026-05-01応募済み")
        assert n == 1

        row = db_conn.execute(
            "SELECT outcome, outcome_at, outcome_note FROM judgments "
            "WHERE bid_id = ?", (seeded_bid,)
        ).fetchone()
        assert row['outcome'] == "applied"
        assert row['outcome_at'] is not None
        assert row['outcome_note'] == "2026-05-01応募済み"

    def test_outcome_attached_to_latest(self, db_conn, seeded_bid):
        # 古い→新しい順に2件入れる
        first_id = insert_judgment(
            db_conn, seeded_bid,
            BidJudgment(verdict="skip", confidence=70, reason="x"),
        )
        # 一旦 sleep 不要 (id ASC でも整列するように record 側はjudged_at DESC, id DESC)
        latest_id = insert_judgment(
            db_conn, seeded_bid,
            BidJudgment(verdict="apply", confidence=80, reason="y"),
        )
        record_judgment_outcome(db_conn, seeded_bid, "applied")

        rows = db_conn.execute(
            "SELECT id, outcome FROM judgments WHERE bid_id = ? ORDER BY id",
            (seeded_bid,),
        ).fetchall()
        assert rows[0]['id'] == first_id
        assert rows[0]['outcome'] is None
        assert rows[1]['id'] == latest_id
        assert rows[1]['outcome'] == "applied"

    def test_invalid_outcome_raises(self, db_conn, seeded_bid):
        insert_judgment(
            db_conn, seeded_bid,
            BidJudgment(verdict="apply", confidence=80, reason="x"),
        )
        with pytest.raises(ValueError):
            record_judgment_outcome(db_conn, seeded_bid, "maybe")

    def test_no_judgment_returns_zero(self, db_conn, seeded_bid):
        # judgmentsを入れずに outcome 試行
        n = record_judgment_outcome(db_conn, seeded_bid, "applied")
        assert n == 0


class TestJudgmentStats:
    def test_empty_db(self, db_conn):
        s = get_judgment_stats(db_conn)
        assert s['total'] == 0
        assert s['by_verdict'] == {}
        assert s['with_outcome'] == 0
        assert s['accuracy'] is None

    def test_verdict_distribution(self, db_conn, seeded_bid):
        for v in ("apply", "apply", "skip", "uncertain"):
            insert_judgment(
                db_conn, seeded_bid,
                BidJudgment(verdict=v, confidence=60, reason="x"),
            )
        s = get_judgment_stats(db_conn)
        assert s['total'] == 4
        assert s['by_verdict']['apply'] == 2
        assert s['by_verdict']['skip'] == 1
        assert s['by_verdict']['uncertain'] == 1

    def test_accuracy_with_feedback(self, db_conn):
        # 2 bids
        insert_municipality(db_conn, {
            'code': '362018', 'name': '松前町', 'prefecture': '愛媛県',
            'region': '四国', 'population': 28000,
            'bid_page_url': None, 'news_page_url': None,
            'page_type': 'unknown', 'active': 1,
        })
        for i in range(4):
            insert_bid(db_conn, {
                'municipality_code': '362018',
                'title': f'案件{i}', 'url': f'https://example.jp/{i}',
                'published_date': None, 'deadline': None,
                'bid_type': 'proposal', 'budget_amount': None,
                'source': 'municipal_hp', 'raw_text': '',
            })
        bid_ids = [
            r['id'] for r in db_conn.execute(
                "SELECT id FROM bids ORDER BY id"
            ).fetchall()
        ]

        # apply→applied (正解), apply→skipped (不正解),
        # skip→skipped (正解), skip→applied (不正解)
        cases = [
            (bid_ids[0], "apply", "applied"),
            (bid_ids[1], "apply", "skipped"),
            (bid_ids[2], "skip", "skipped"),
            (bid_ids[3], "skip", "applied"),
        ]
        for bid_id, verdict, outcome in cases:
            insert_judgment(
                db_conn, bid_id,
                BidJudgment(verdict=verdict, confidence=70, reason="x"),
            )
            record_judgment_outcome(db_conn, bid_id, outcome)

        s = get_judgment_stats(db_conn)
        assert s['with_outcome'] == 4
        # 正解: apply→applied + skip→skipped = 2件 / 4件 = 50%
        assert s['accuracy'] == 0.5
        assert s['agreement'][("apply", "applied")] == 1
        assert s['agreement'][("skip", "skipped")] == 1

    def test_accuracy_won_counts_as_apply_correct(self, db_conn, seeded_bid):
        """outcome=won も apply判定の正解とみなす。"""
        insert_judgment(
            db_conn, seeded_bid,
            BidJudgment(verdict="apply", confidence=80, reason="x"),
        )
        record_judgment_outcome(db_conn, seeded_bid, "won")
        s = get_judgment_stats(db_conn)
        assert s['accuracy'] == 1.0
