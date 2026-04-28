"""LLM-based bid evaluator agent.

Decides whether 当社 should apply to a given municipal bid.

Two modes:
- Simple (`use_tools=false`): single Claude call, uses only the listing text.
- Agent (`use_tools=true`): multi-turn tool use loop. Claude can call
  `fetch_bid_detail` (HTTP fetch) and `search_past_bids` (SQL on local DB)
  before returning the verdict.

Cost control: only top-N bids per run are evaluated (`max_judgments_per_run`).
Prompt caching is applied to the system prompt to amortize the company-context
across all bids in a run.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """\
あなたは当社の入札応募判断アシスタントです。

# 当社プロファイル
- データ分析・BI・DX支援を行う小規模法人
- メンバー1-2名、実績まだ少なめ
- 得意領域: データ可視化(BIダッシュボード)、統計分析、業務データ集計、KPIモニタリング基盤、Looker Studio/Tableau/Power BI実装
- 苦手・対象外: 大規模システム開発、ハードウェア構築、土木・建設、印刷物・物流、医療系、24時間運用が前提の案件

# あなたの仕事
案件1件について「当社が応募すべきか」を評価し、最後にJSONで出力する。

# 評価軸
1. 業務領域がデータ分析/BI/DX/可視化/集計/調査と合致するか
2. 想定工数が小〜中規模(3人月以内)に収まりそうか
3. 資格要件が緩いか(実績不問・法人格のみ等)
4. 提出 / 対応の難易度

# ツールの使い方
- 公告タイトルとマッチキーワードだけで判断材料が十分なら、ツールを呼ばずに判定してよい (低コスト優先)
- 「業務範囲が曖昧」「予算規模を知りたい」「要件を確認したい」と感じた時のみ `fetch_bid_detail` で詳細ページを取得
- 同じ自治体の過去傾向を知りたい時のみ `search_past_bids` を使う
- ツール呼び出しは最大3回まで。それ以上必要なら手元情報で判断する

# 最終出力 (JSON、コードブロック不要)
{
  "verdict": "apply" or "skip" or "uncertain",
  "confidence": 0-100の整数,
  "reason": "60-120字で簡潔に",
  "estimated_effort": "X人月" or "不明",
  "concerns": ["懸念点1", "懸念点2"]
}

JSON以外のテキストを最終応答に混ぜない (前置き・解説不要)。"""


def _format_user_message(bid: dict, matched_keywords: List[str]) -> str:
    return f"""\
以下の案件を評価してください。

# 案件情報
- 案件名: {bid.get('title', '')}
- 自治体: {bid.get('municipality_name') or bid.get('name', '不明')}（{bid.get('prefecture', '')}）
- 種別: {bid.get('bid_type', 'unknown')}
- 公告日: {bid.get('published_date') or '不明'}
- マッチキーワード: {', '.join(matched_keywords) if matched_keywords else 'なし'}
- 案件URL: {bid.get('url', '')}
- 公告本文(抜粋): {(bid.get('raw_text') or '')[:1500]}"""


@dataclass
class BidJudgment:
    verdict: str = "uncertain"
    confidence: int = 0
    reason: str = ""
    estimated_effort: str = "不明"
    concerns: List[str] = field(default_factory=list)
    tool_calls: int = 0  # number of tool invocations during judging

    @classmethod
    def empty(cls, reason: str = "") -> "BidJudgment":
        return cls(verdict="uncertain", confidence=0, reason=reason)

    @property
    def is_empty(self) -> bool:
        return self.verdict == "uncertain" and self.confidence == 0


class BidJudge:
    """LLM応募可否判定エージェント。

    config.yaml `llm.enabled: true` かつ ANTHROPIC_API_KEY がある場合のみ動作。
    `llm.use_tools: true` で agentic ループモード (デフォルト true)。
    """

    def __init__(self, config: Optional[dict] = None):
        if config is None:
            from scraper.base import load_config
            config = load_config()
        llm_config = config.get("llm", {}) or {}
        self.enabled = bool(llm_config.get("enabled", False))
        self.model = llm_config.get("model", "claude-haiku-4-5")
        self.max_judgments = int(llm_config.get("max_judgments_per_run", 10))
        self.max_tokens = int(llm_config.get("max_tokens", 1024))
        self.use_tools = bool(llm_config.get("use_tools", True))
        self.max_tool_iterations = int(llm_config.get("max_tool_iterations", 4))
        self.api_key = os.environ.get("ANTHROPIC_API_KEY", "")

        self._client = None
        if self.enabled and self.api_key:
            try:
                from anthropic import Anthropic
                self._client = Anthropic(api_key=self.api_key)
            except ImportError:
                logger.warning(
                    "anthropic package not installed; LLM judge disabled. "
                    "Run: pip install anthropic"
                )
                self.enabled = False
        elif self.enabled and not self.api_key:
            logger.warning("LLM enabled but ANTHROPIC_API_KEY not set; LLM judge disabled")
            self.enabled = False

    @property
    def is_configured(self) -> bool:
        return self.enabled and self._client is not None

    # --- public API ---

    def judge(
        self,
        bid: dict,
        matched_keywords: List[str],
        executor=None,
    ) -> BidJudgment:
        """1案件を評価。

        Args:
            bid: 案件dict
            matched_keywords: ルールフィルタでヒットしたキーワード
            executor: JudgeToolExecutor (use_tools=True の時のみ使用)
        """
        if not self.is_configured:
            return BidJudgment.empty("LLM未設定")

        if self.use_tools and executor is not None:
            return self._judge_with_tools(bid, matched_keywords, executor)
        return self._judge_simple(bid, matched_keywords)

    def judge_batch(
        self,
        notify_targets: list,
        executor=None,
    ) -> list:
        """notify_targets [(bid, score, matched), ...] にLLM判定を加えて返す。

        スコア上位 max_judgments_per_run 件のみ判定。残りは empty 判定。

        Returns:
            list of (bid, score, matched, judgment) tuples
        """
        results = []
        target_count = min(self.max_judgments, len(notify_targets))
        for i, item in enumerate(notify_targets):
            bid, score, matched = item[0], item[1], item[2]
            if i < self.max_judgments and self.is_configured:
                judgment = self.judge(bid, matched, executor=executor)
                logger.info(
                    f"AI判定 [{i+1}/{target_count}] "
                    f"verdict={judgment.verdict} conf={judgment.confidence} "
                    f"tool_calls={judgment.tool_calls}: "
                    f"{(bid.get('title') or '')[:40]}"
                )
            else:
                judgment = BidJudgment.empty("対象外(上位N件のみ判定)")
            results.append((bid, score, matched, judgment))
        return results

    # --- internal: simple one-shot ---

    def _judge_simple(self, bid: dict, matched_keywords: List[str]) -> BidJudgment:
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[
                    {"role": "user", "content": _format_user_message(bid, matched_keywords)}
                ],
            )
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            return BidJudgment.empty(f"LLM呼び出し失敗: {type(e).__name__}")

        text = "".join(b.text for b in response.content if b.type == "text")
        return self._parse(text)

    # --- internal: agentic tool-use loop ---

    def _judge_with_tools(self, bid: dict, matched_keywords, executor) -> BidJudgment:
        from judge.tools import TOOL_SCHEMAS

        messages = [
            {"role": "user", "content": _format_user_message(bid, matched_keywords)}
        ]
        tool_calls = 0

        for iteration in range(self.max_tool_iterations):
            try:
                response = self._client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=[
                        {
                            "type": "text",
                            "text": SYSTEM_PROMPT,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    tools=TOOL_SCHEMAS,
                    messages=messages,
                )
            except Exception as e:
                logger.error(f"LLM call failed (iteration {iteration}): {e}")
                return BidJudgment.empty(f"LLM呼び出し失敗: {type(e).__name__}")

            if response.stop_reason == "end_turn":
                text = "".join(b.text for b in response.content if b.type == "text")
                judgment = self._parse(text)
                judgment.tool_calls = tool_calls
                return judgment

            if response.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": response.content})
                tool_results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    tool_calls += 1
                    logger.debug(
                        f"  tool_use: {block.name}({block.input})"
                    )
                    try:
                        result = executor.execute(block.name, dict(block.input))
                    except Exception as e:
                        result = f"Error: tool execution raised {type(e).__name__}: {e}"
                        logger.warning(f"Tool execution error: {e}")
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        }
                    )
                messages.append({"role": "user", "content": tool_results})
                continue

            # max_tokens, refusal, etc.
            logger.warning(
                f"Unexpected stop_reason: {response.stop_reason} (iteration {iteration})"
            )
            return BidJudgment.empty(f"予期しない stop_reason: {response.stop_reason}")

        logger.warning(
            f"Tool iteration limit ({self.max_tool_iterations}) reached without verdict"
        )
        return BidJudgment.empty("ツール使用上限到達")

    # --- JSON parsing (defensive) ---

    @staticmethod
    def _parse(text: str) -> BidJudgment:
        text = (text or "").strip()
        if text.startswith("```"):
            lines = [l for l in text.split("\n") if not l.startswith("```")]
            text = "\n".join(lines).strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    data = json.loads(text[start : end + 1])
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse LLM response as JSON: {e}\n{text[:500]}")
                    return BidJudgment.empty("JSONパース失敗")
            else:
                logger.error(f"No JSON object found in LLM response:\n{text[:500]}")
                return BidJudgment.empty("JSONパース失敗")

        verdict = data.get("verdict", "uncertain")
        if verdict not in ("apply", "skip", "uncertain"):
            verdict = "uncertain"

        try:
            confidence = int(data.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0
        confidence = max(0, min(100, confidence))

        concerns = data.get("concerns", []) or []
        if not isinstance(concerns, list):
            concerns = [str(concerns)]

        return BidJudgment(
            verdict=verdict,
            confidence=confidence,
            reason=str(data.get("reason", "")),
            estimated_effort=str(data.get("estimated_effort", "不明")),
            concerns=[str(c) for c in concerns],
        )
