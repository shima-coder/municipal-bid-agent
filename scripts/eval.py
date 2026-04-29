"""判定エージェントの評価ハーネス。

`judgments` テーブルの outcome 付き判定をテストデータとみなし、
- offline モード: 過去の判定をそのまま使って混同行列・指標を計算 (API呼び出しなし)
- rerun モード:  outcome 付き案件を現プロンプトで再判定して、過去判定との一致率と
                 outcome 一致率を計算 (プロンプト変更の影響を測定するのに使う)

使い方:
    python scripts/eval.py                     # offline
    python scripts/eval.py --rerun             # 再判定 (要 ANTHROPIC_API_KEY)
    python scripts/eval.py --rerun --limit 20  # 最大20件まで再判定 (コスト制御)
"""

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from db.models import get_connection, init_db
from db.store import get_judgment_stats, import_municipalities_from_json
from judge.llm import BidJudge
from judge.tools import JudgeToolExecutor
from scraper.base import load_config


def offline_metrics(conn):
    """API呼び出しなしで、保存済み judgment + outcome から指標を計算する。"""
    stats = get_judgment_stats(conn)
    rows = conn.execute(
        """
        SELECT verdict, outcome, COUNT(*) as n
        FROM judgments
        WHERE outcome IS NOT NULL
        GROUP BY verdict, outcome
        """
    ).fetchall()
    return stats, rows


def print_confusion(stats):
    """verdict × outcome 混同行列をきれいに出力する。"""
    print("=" * 64)
    print(" 評価サマリ (offline)")
    print("=" * 64)
    print(f" 総判定数:                {stats['total']}")
    print(f" verdict 内訳:")
    for v in ("apply", "skip", "uncertain"):
        print(f"   {v:10s}: {stats['by_verdict'].get(v, 0)}")
    print(f" outcome 付き判定:        {stats['with_outcome']}")
    if stats["accuracy"] is not None:
        print(f" 単純精度 (apply→applied/won, skip→skipped):  {stats['accuracy']:.1%}")
        print()
        print(" 混同行列 (rows=verdict, cols=outcome):")
        outcomes = ("applied", "skipped", "won", "lost")
        verdicts = ("apply", "skip", "uncertain")
        header = "          " + "".join(f"{o:>10s}" for o in outcomes)
        print(header)
        for v in verdicts:
            row = f"   {v:8s}"
            for o in outcomes:
                row += f"{stats['agreement'].get((v, o), 0):>10d}"
            print(row)
    else:
        print(" → outcome 付き判定がまだないので精度計算なし")
    print("=" * 64)


def rerun_eval(conn, limit, model_override=None):
    """outcome 付き案件を現プロンプトで再判定し、結果を返す。

    Returns:
        list of dict: 各レコードに 過去verdict / 新verdict / outcome / 一致情報
    """
    rows = conn.execute(
        """
        SELECT j.bid_id, j.verdict AS prev_verdict, j.confidence AS prev_confidence,
               j.outcome, j.outcome_note,
               b.title, b.url, b.bid_type, b.published_date,
               b.matched_keywords, b.raw_text,
               m.name AS municipality_name, m.prefecture
        FROM judgments j
        JOIN bids b ON b.id = j.bid_id
        LEFT JOIN municipalities m ON m.code = b.municipality_code
        WHERE j.outcome IS NOT NULL
        ORDER BY j.judged_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    if not rows:
        return []

    config = load_config()
    config.setdefault("llm", {})
    config["llm"]["enabled"] = True
    config["llm"]["use_tools"] = True
    if model_override:
        config["llm"]["model"] = model_override

    judge = BidJudge(config)
    if not judge.is_configured:
        print("ERROR: judge not configured (set ANTHROPIC_API_KEY)", file=sys.stderr)
        sys.exit(1)

    http_session = requests.Session()
    http_session.headers.update({"User-Agent": "MunicipalBidAgent/1.0 (eval)"})
    executor = JudgeToolExecutor(http_session=http_session, db_conn=conn)

    results = []
    for r in rows:
        bid = dict(r)
        matched = (bid.get("matched_keywords") or "").split(",")
        matched = [k for k in matched if k]

        new_judgment = judge.judge(bid, matched, executor=executor)
        # outcome との一致 (apply→applied/won, skip→skipped を正解)
        outcome = bid["outcome"]
        new_correct = (
            (new_judgment.verdict == "apply" and outcome in ("applied", "won"))
            or (new_judgment.verdict == "skip" and outcome == "skipped")
        )
        prev_correct = (
            (bid["prev_verdict"] == "apply" and outcome in ("applied", "won"))
            or (bid["prev_verdict"] == "skip" and outcome == "skipped")
        )
        results.append(
            {
                "bid_id": bid["bid_id"],
                "title": (bid["title"] or "")[:50],
                "outcome": outcome,
                "prev_verdict": bid["prev_verdict"],
                "prev_confidence": bid["prev_confidence"],
                "new_verdict": new_judgment.verdict,
                "new_confidence": new_judgment.confidence,
                "new_tool_calls": new_judgment.tool_calls,
                "prev_correct": prev_correct,
                "new_correct": new_correct,
            }
        )
    return results


def print_rerun_results(results):
    print("=" * 64)
    print(f" 再判定 ({len(results)}件)")
    print("=" * 64)
    if not results:
        print(" outcome 付き判定がないので再判定対象なし")
        print(" --feedback で outcome を記録してから再実行してください")
        print("=" * 64)
        return

    for i, r in enumerate(results, 1):
        flag_prev = "✓" if r["prev_correct"] else "✗"
        flag_new = "✓" if r["new_correct"] else "✗"
        improved = "→改善" if (not r["prev_correct"]) and r["new_correct"] else (
            "→悪化" if r["prev_correct"] and not r["new_correct"] else "(同じ)"
        )
        print(
            f" [{i:2d}] bid_id={r['bid_id']}  outcome={r['outcome']:8s}\n"
            f"      旧 {r['prev_verdict']:10s} (conf {r['prev_confidence']:3d}) {flag_prev}  "
            f"→  新 {r['new_verdict']:10s} (conf {r['new_confidence']:3d}) {flag_new}  {improved}\n"
            f"      {r['title']}"
        )

    prev_acc = sum(1 for r in results if r["prev_correct"]) / len(results)
    new_acc = sum(1 for r in results if r["new_correct"]) / len(results)
    delta = new_acc - prev_acc
    arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
    print()
    print(f" 旧プロンプト精度: {prev_acc:.1%}")
    print(f" 新プロンプト精度: {new_acc:.1%}  {arrow} {delta:+.1%}")
    print("=" * 64)


def main():
    parser = argparse.ArgumentParser(description="判定エージェント評価ハーネス")
    parser.add_argument(
        "--rerun", action="store_true",
        help="outcome 付き案件を現プロンプトで再判定する (要 API key)",
    )
    parser.add_argument(
        "--limit", type=int, default=20,
        help="再判定の最大件数 (default 20、コスト制御)",
    )
    parser.add_argument(
        "--model", type=str, default=None,
        help="再判定で使うモデル (例: claude-sonnet-4-6 でアップグレード比較)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="再判定結果を JSON で出力",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    init_db()
    conn = get_connection()
    import_municipalities_from_json(conn)

    if args.rerun:
        results = rerun_eval(conn, args.limit, model_override=args.model)
        if args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
        else:
            print_rerun_results(results)
    else:
        stats, _ = offline_metrics(conn)
        print_confusion(stats)

    conn.close()


if __name__ == "__main__":
    main()
