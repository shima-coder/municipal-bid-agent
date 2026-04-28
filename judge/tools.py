"""Tools that the LLM judge can call to research a bid before deciding.

Exposed to Claude as tool definitions; executed by `JudgeToolExecutor`.
"""

import logging
from typing import Optional

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


# Tool schemas for the Anthropic Messages API tool_use surface
TOOL_SCHEMAS = [
    {
        "name": "fetch_bid_detail",
        "description": (
            "案件詳細ページのHTMLを取得し、本文テキストを返す。"
            "公告概要だけでは応募可否を判断できない時に使う"
            "(要件・想定予算・締切などを確認したい場合)。"
            "PDF直リンクの場合はその旨を返す。fetch失敗時はエラー文字列を返す。"
            "本文は最大5000文字に切り詰める。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "案件詳細ページの絶対URL",
                }
            },
            "required": ["url"],
        },
    },
    {
        "name": "search_past_bids",
        "description": (
            "同じ自治体の過去案件をローカルDBから検索する。"
            "その自治体の発注傾向・案件規模・繰り返し発注の有無を把握したい場合のみ使う。"
            "新しい自治体や情報が乏しい時は呼ばない方がよい。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "municipality_name": {
                    "type": "string",
                    "description": "自治体名（例: 松前町）。都道府県は含めない",
                },
                "limit": {
                    "type": "integer",
                    "description": "取得件数。デフォルト5、最大10",
                },
            },
            "required": ["municipality_name"],
        },
    },
]


class JudgeToolExecutor:
    """LLMが要求したツール呼び出しを実行する。

    DB接続とHTTPセッションをコンストラクタで受け取る (依存注入)。
    HTTPセッションが無い場合は fetch ツールはエラーを返す。
    """

    def __init__(
        self,
        http_session=None,
        db_conn=None,
        max_fetch_chars: int = 5000,
        timeout: int = 15,
    ):
        self.http_session = http_session
        self.db_conn = db_conn
        self.max_fetch_chars = max_fetch_chars
        self.timeout = timeout

    def execute(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "fetch_bid_detail":
            return self._fetch_bid_detail(tool_input.get("url", ""))
        if tool_name == "search_past_bids":
            return self._search_past_bids(
                tool_input.get("municipality_name", ""),
                tool_input.get("limit", 5),
            )
        return f"Error: unknown tool '{tool_name}'"

    # --- tool implementations ---

    def _fetch_bid_detail(self, url: str) -> str:
        if not url:
            return "Error: URL not provided"

        if url.lower().endswith(".pdf"):
            return f"PDF直リンク: {url} (このツールではPDF解析未対応。タイトルとURLからの判断に留めてください)"

        if self.http_session is None:
            return "Error: HTTP session not available in this context"

        try:
            resp = self.http_session.get(url, timeout=self.timeout, verify=True)
        except Exception:
            try:
                resp = self.http_session.get(url, timeout=self.timeout, verify=False)
            except Exception as e:
                return f"Error: fetch failed ({type(e).__name__}: {e})"

        if resp.status_code != 200:
            return f"Error: HTTP {resp.status_code} for {url}"

        # Encoding fallback (Shift_JIS / EUC-JP の古いサイト対策)
        if resp.encoding == "ISO-8859-1" or not resp.encoding:
            try:
                import chardet
                detected = chardet.detect(resp.content[:4096])
                if detected.get("encoding"):
                    resp.encoding = detected["encoding"]
            except Exception:
                resp.encoding = "utf-8"

        try:
            soup = BeautifulSoup(resp.text, "lxml")
        except Exception as e:
            return f"Error: HTMLパース失敗 ({type(e).__name__}: {e})"

        for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)
        if not text:
            return "Error: 本文を抽出できませんでした"

        if len(text) > self.max_fetch_chars:
            text = text[: self.max_fetch_chars] + f"\n\n... (truncated at {self.max_fetch_chars} chars)"

        return text

    def _search_past_bids(self, municipality_name: str, limit) -> str:
        if not municipality_name:
            return "Error: municipality_name required"
        if self.db_conn is None:
            return "Error: DB connection not available in this context"

        try:
            limit_int = int(limit) if limit is not None else 5
        except (TypeError, ValueError):
            limit_int = 5
        limit_int = max(1, min(10, limit_int))

        try:
            rows = self.db_conn.execute(
                """
                SELECT b.title, b.bid_type, b.published_date, b.filter_score,
                       b.matched_keywords, b.url
                FROM bids b
                LEFT JOIN municipalities m ON b.municipality_code = m.code
                WHERE m.name = ?
                ORDER BY b.created_at DESC
                LIMIT ?
                """,
                (municipality_name, limit_int),
            ).fetchall()
        except Exception as e:
            return f"Error: DB query failed ({type(e).__name__}: {e})"

        if not rows:
            return f"{municipality_name} の過去案件はDBに見つかりませんでした。"

        lines = [f"{municipality_name} の過去案件 ({len(rows)}件):"]
        for r in rows:
            d = dict(r)
            lines.append(
                f"- [{d.get('bid_type') or '?'}] {d.get('title') or '?'} "
                f"(公告日: {d.get('published_date') or '不明'}, "
                f"スコア: {d.get('filter_score') or 0}, "
                f"キーワード: {d.get('matched_keywords') or 'なし'})"
            )
        return "\n".join(lines)
