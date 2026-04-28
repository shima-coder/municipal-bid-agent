"""LLM-based bid evaluator — decides whether 当社 should apply."""

import json
import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


PROMPT_TEMPLATE = """\
あなたは当社（データ分析・BI・DX支援を行う小規模法人。メンバー1-2名、実績まだ少なめ）の入札応募判断アシスタントです。
以下の自治体公告を読み、当社が応募すべきかを評価してください。

# 公告情報
- 案件名: {title}
- 自治体: {municipality_name}（{prefecture}）
- 種別: {bid_type}
- 公告日: {published_date}
- マッチキーワード: {matched_keywords}
- 案件URL: {url}
- 公告本文(抜粋): {raw_text}

# 評価軸
1. 業務領域がデータ分析 / BI / DX / 可視化 / 集計 / 調査と合致するか
2. 想定工数が小〜中規模(3人月以内)に収まりそうか
3. 資格要件が緩いか(実績不問・法人格のみ等)
4. 提出 / 対応の難易度

# 出力 (JSON、コードブロックなし)
{{
  "verdict": "apply" or "skip" or "uncertain",
  "confidence": 0-100の整数,
  "reason": "60-120字で簡潔に",
  "estimated_effort": "X人月" or "不明",
  "concerns": ["懸念点1", "懸念点2"]
}}

JSONのみ出力。前置き・解説は不要。"""


@dataclass
class BidJudgment:
    verdict: str = "uncertain"
    confidence: int = 0
    reason: str = ""
    estimated_effort: str = "不明"
    concerns: List[str] = field(default_factory=list)

    @classmethod
    def empty(cls, reason: str = "") -> "BidJudgment":
        return cls(verdict="uncertain", confidence=0, reason=reason)

    @property
    def is_empty(self) -> bool:
        return self.verdict == "uncertain" and self.confidence == 0


class BidJudge:
    """LLMで案件の応募可否を判断するエージェント。

    config.yaml の `llm.enabled: true` かつ環境変数 `ANTHROPIC_API_KEY` がある場合のみ動作。
    未設定時は empty 判定を返してパイプラインを壊さない。
    """

    def __init__(self, config: Optional[dict] = None):
        if config is None:
            from scraper.base import load_config
            config = load_config()
        llm_config = config.get("llm", {}) or {}
        self.enabled = bool(llm_config.get("enabled", False))
        self.model = llm_config.get("model", "claude-haiku-4-5-20251001")
        self.max_judgments = int(llm_config.get("max_judgments_per_run", 10))
        self.max_tokens = int(llm_config.get("max_tokens", 1024))
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

    def judge(self, bid: dict, matched_keywords: List[str]) -> BidJudgment:
        """1案件をLLMで評価する。"""
        if not self.is_configured:
            return BidJudgment.empty("LLM未設定")

        prompt = PROMPT_TEMPLATE.format(
            title=bid.get("title", ""),
            municipality_name=bid.get("municipality_name") or bid.get("name", "不明"),
            prefecture=bid.get("prefecture", ""),
            bid_type=bid.get("bid_type", "unknown"),
            published_date=bid.get("published_date") or "不明",
            matched_keywords=", ".join(matched_keywords) if matched_keywords else "なし",
            url=bid.get("url", ""),
            raw_text=(bid.get("raw_text") or "")[:1500],
        )

        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text
            return self._parse(text)
        except Exception as e:
            logger.error(f"LLM judge failed: {e}")
            return BidJudgment.empty(f"LLM呼び出し失敗: {type(e).__name__}")

    def judge_batch(self, notify_targets: list) -> list:
        """notify_targets [(bid, score, matched), ...] に LLM 判定を加えて返す。

        スコア上位 max_judgments 件のみLLM呼び出し。残りは empty 判定。

        Returns:
            list of (bid, score, matched, judgment) tuples
        """
        results = []
        for i, item in enumerate(notify_targets):
            bid, score, matched = item[0], item[1], item[2]
            if i < self.max_judgments and self.is_configured:
                judgment = self.judge(bid, matched)
                logger.info(
                    f"AI判定 [{i+1}/{min(self.max_judgments, len(notify_targets))}] "
                    f"verdict={judgment.verdict} confidence={judgment.confidence} "
                    f": {bid.get('title', '')[:40]}"
                )
            else:
                judgment = BidJudgment.empty("対象外(上位N件のみ判定)")
            results.append((bid, score, matched, judgment))
        return results

    @staticmethod
    def _parse(text: str) -> BidJudgment:
        text = text.strip()
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
