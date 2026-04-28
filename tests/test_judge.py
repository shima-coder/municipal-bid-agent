"""LLM judge agent tests."""

import os
from unittest.mock import MagicMock, patch

import pytest

from judge.llm import BidJudge, BidJudgment


@pytest.fixture
def disabled_config():
    return {"llm": {"enabled": False}}


@pytest.fixture
def enabled_config():
    return {
        "llm": {
            "enabled": True,
            "model": "claude-haiku-4-5-20251001",
            "max_judgments_per_run": 3,
            "max_tokens": 512,
        }
    }


@pytest.fixture
def sample_bid():
    return {
        "title": "データ分析業務委託",
        "municipality_name": "松前町",
        "prefecture": "愛媛県",
        "bid_type": "proposal",
        "published_date": "2026-04-20",
        "url": "https://example.jp/bid/123",
        "raw_text": "データ可視化と統計分析を含む業務",
    }


class TestBidJudgmentDataclass:
    def test_empty_factory(self):
        j = BidJudgment.empty("理由")
        assert j.verdict == "uncertain"
        assert j.confidence == 0
        assert j.reason == "理由"
        assert j.is_empty is True

    def test_non_empty(self):
        j = BidJudgment(verdict="apply", confidence=80, reason="ok")
        assert j.is_empty is False


class TestBidJudgeConfigGating:
    def test_disabled_in_config(self, disabled_config):
        judge = BidJudge(disabled_config)
        assert judge.is_configured is False

    def test_enabled_but_no_api_key(self, enabled_config, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        judge = BidJudge(enabled_config)
        assert judge.is_configured is False

    def test_judge_returns_empty_when_unconfigured(self, disabled_config, sample_bid):
        judge = BidJudge(disabled_config)
        result = judge.judge(sample_bid, ["データ分析"])
        assert result.is_empty is True

    def test_judge_batch_returns_4tuples_with_empty_judgments(
        self, disabled_config, sample_bid
    ):
        judge = BidJudge(disabled_config)
        targets = [(sample_bid, 5, ["データ分析"])]
        results = judge.judge_batch(targets)
        assert len(results) == 1
        bid, score, matched, judgment = results[0]
        assert bid == sample_bid
        assert score == 5
        assert matched == ["データ分析"]
        assert isinstance(judgment, BidJudgment)
        assert judgment.is_empty is True


class TestParse:
    def test_parse_clean_json(self):
        text = '{"verdict": "apply", "confidence": 80, "reason": "領域一致", "estimated_effort": "1人月", "concerns": ["短納期"]}'
        j = BidJudge._parse(text)
        assert j.verdict == "apply"
        assert j.confidence == 80
        assert j.reason == "領域一致"
        assert j.estimated_effort == "1人月"
        assert j.concerns == ["短納期"]

    def test_parse_code_fenced(self):
        text = '```json\n{"verdict": "skip", "confidence": 70, "reason": "領域外"}\n```'
        j = BidJudge._parse(text)
        assert j.verdict == "skip"
        assert j.confidence == 70

    def test_parse_with_preamble(self):
        text = 'はい、評価します。\n{"verdict": "apply", "confidence": 60, "reason": "ok"}'
        j = BidJudge._parse(text)
        assert j.verdict == "apply"
        assert j.confidence == 60

    def test_parse_invalid_verdict_normalized(self):
        text = '{"verdict": "yes", "confidence": 50, "reason": "x"}'
        j = BidJudge._parse(text)
        assert j.verdict == "uncertain"

    def test_parse_confidence_clamped(self):
        text = '{"verdict": "apply", "confidence": 150, "reason": "x"}'
        j = BidJudge._parse(text)
        assert j.confidence == 100

    def test_parse_garbage_returns_empty(self):
        j = BidJudge._parse("これはJSONじゃないテキスト")
        assert j.is_empty is True

    def test_parse_concerns_string_coerced_to_list(self):
        text = '{"verdict": "skip", "confidence": 60, "reason": "x", "concerns": "短納期"}'
        j = BidJudge._parse(text)
        assert j.concerns == ["短納期"]


class TestJudgeWithMockedClient:
    def test_judge_calls_anthropic(self, enabled_config, sample_bid, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(text='{"verdict": "apply", "confidence": 85, "reason": "領域一致", "estimated_effort": "1人月", "concerns": []}')
        ]

        with patch("anthropic.Anthropic") as mock_anthropic:
            instance = mock_anthropic.return_value
            instance.messages.create.return_value = mock_response

            judge = BidJudge(enabled_config)
            assert judge.is_configured is True

            result = judge.judge(sample_bid, ["データ分析"])
            assert result.verdict == "apply"
            assert result.confidence == 85

            # APIが呼ばれたことの確認
            instance.messages.create.assert_called_once()
            call_kwargs = instance.messages.create.call_args.kwargs
            assert call_kwargs["model"] == "claude-haiku-4-5-20251001"
            # プロンプトに案件タイトルが含まれる
            user_msg = call_kwargs["messages"][0]["content"]
            assert "データ分析業務委託" in user_msg
            assert "松前町" in user_msg

    def test_judge_handles_api_error(self, enabled_config, sample_bid, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        with patch("anthropic.Anthropic") as mock_anthropic:
            instance = mock_anthropic.return_value
            instance.messages.create.side_effect = RuntimeError("API down")

            judge = BidJudge(enabled_config)
            result = judge.judge(sample_bid, ["データ分析"])
            assert result.is_empty is True
            assert "失敗" in result.reason

    def test_judge_batch_respects_max_judgments(
        self, enabled_config, sample_bid, monkeypatch
    ):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(text='{"verdict": "apply", "confidence": 70, "reason": "ok"}')
        ]

        with patch("anthropic.Anthropic") as mock_anthropic:
            instance = mock_anthropic.return_value
            instance.messages.create.return_value = mock_response

            judge = BidJudge(enabled_config)  # max_judgments_per_run = 3
            targets = [(sample_bid, 10 - i, ["データ分析"]) for i in range(5)]
            results = judge.judge_batch(targets)

            assert len(results) == 5
            # 上位3件のみAPI呼び出し
            assert instance.messages.create.call_count == 3
            # 上位3件は判定あり、4-5件目はempty
            assert results[0][3].verdict == "apply"
            assert results[2][3].verdict == "apply"
            assert results[3][3].is_empty is True
            assert results[4][3].is_empty is True
