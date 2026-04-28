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
            "model": "claude-haiku-4-5",
            "use_tools": False,  # default to simple mode for existing tests
            "max_judgments_per_run": 3,
            "max_tokens": 512,
        }
    }


@pytest.fixture
def enabled_config_with_tools():
    return {
        "llm": {
            "enabled": True,
            "model": "claude-haiku-4-5",
            "use_tools": True,
            "max_judgments_per_run": 3,
            "max_tool_iterations": 3,
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
        mock_response.stop_reason = "end_turn"
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = '{"verdict": "apply", "confidence": 85, "reason": "領域一致", "estimated_effort": "1人月", "concerns": []}'
        mock_response.content = [text_block]

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
            assert call_kwargs["model"] == "claude-haiku-4-5"
            # プロンプトに案件タイトルが含まれる
            user_msg = call_kwargs["messages"][0]["content"]
            assert "データ分析業務委託" in user_msg
            assert "松前町" in user_msg
            # システムプロンプトに会社プロファイルが入っている (cache_control付き)
            system = call_kwargs["system"]
            assert isinstance(system, list)
            assert system[0]["cache_control"]["type"] == "ephemeral"
            assert "当社" in system[0]["text"]

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
        mock_response.stop_reason = "end_turn"
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = '{"verdict": "apply", "confidence": 70, "reason": "ok"}'
        mock_response.content = [text_block]

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


class TestJudgeWithToolUseLoop:
    """Agentic ツール使用ループ (use_tools=True) のテスト。"""

    def _make_text_response(self, text, stop_reason="end_turn"):
        msg = MagicMock()
        msg.stop_reason = stop_reason
        block = MagicMock()
        block.type = "text"
        block.text = text
        msg.content = [block]
        return msg

    def _make_tool_use_response(self, tool_name, tool_input, tool_id="tu_1"):
        msg = MagicMock()
        msg.stop_reason = "tool_use"
        block = MagicMock()
        block.type = "tool_use"
        block.name = tool_name
        block.input = tool_input
        block.id = tool_id
        msg.content = [block]
        return msg

    def test_zero_tool_calls_immediate_verdict(
        self, enabled_config_with_tools, sample_bid, monkeypatch
    ):
        """LLMがツールを呼ばずに即verdictを返すケース。"""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        with patch("anthropic.Anthropic") as mock_anthropic:
            instance = mock_anthropic.return_value
            instance.messages.create.return_value = self._make_text_response(
                '{"verdict": "apply", "confidence": 80, "reason": "明確"}'
            )

            executor = MagicMock()
            judge = BidJudge(enabled_config_with_tools)
            result = judge.judge(sample_bid, ["データ分析"], executor=executor)

            assert result.verdict == "apply"
            assert result.tool_calls == 0
            executor.execute.assert_not_called()
            # tools schema が渡されている
            call_kwargs = instance.messages.create.call_args.kwargs
            assert "tools" in call_kwargs
            assert any(t["name"] == "fetch_bid_detail" for t in call_kwargs["tools"])

    def test_one_tool_call_then_verdict(
        self, enabled_config_with_tools, sample_bid, monkeypatch
    ):
        """LLMが fetch_bid_detail を1回呼んでから verdict を返すケース。"""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        with patch("anthropic.Anthropic") as mock_anthropic:
            instance = mock_anthropic.return_value
            instance.messages.create.side_effect = [
                self._make_tool_use_response(
                    "fetch_bid_detail", {"url": "https://example.jp/bid/123"}
                ),
                self._make_text_response(
                    '{"verdict": "skip", "confidence": 60, "reason": "領域外"}'
                ),
            ]

            executor = MagicMock()
            executor.execute.return_value = "公告本文サンプル: 本案件は土木工事です..."

            judge = BidJudge(enabled_config_with_tools)
            result = judge.judge(sample_bid, ["データ分析"], executor=executor)

            assert result.verdict == "skip"
            assert result.tool_calls == 1
            executor.execute.assert_called_once_with(
                "fetch_bid_detail", {"url": "https://example.jp/bid/123"}
            )
            # 2回 messages.create が呼ばれている
            assert instance.messages.create.call_count == 2
            # 2回目の呼び出しでは tool_result が messages に含まれる
            second_call_messages = instance.messages.create.call_args_list[1].kwargs["messages"]
            assert any(
                isinstance(m["content"], list)
                and any(c.get("type") == "tool_result" for c in m["content"])
                for m in second_call_messages
            )

    def test_iteration_limit_returns_empty(
        self, enabled_config_with_tools, sample_bid, monkeypatch
    ):
        """ツール使用ループが上限に達した場合は empty 判定。"""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        with patch("anthropic.Anthropic") as mock_anthropic:
            instance = mock_anthropic.return_value
            # 毎回 tool_use を返してループを回し続ける
            instance.messages.create.return_value = self._make_tool_use_response(
                "fetch_bid_detail", {"url": "https://example.jp/x"}
            )

            executor = MagicMock()
            executor.execute.return_value = "..."

            judge = BidJudge(enabled_config_with_tools)  # max_tool_iterations=3
            result = judge.judge(sample_bid, ["データ分析"], executor=executor)

            assert result.is_empty is True
            assert "上限" in result.reason
            # 上限ぴったり呼ばれる
            assert instance.messages.create.call_count == 3

    def test_tool_execution_error_does_not_crash(
        self, enabled_config_with_tools, sample_bid, monkeypatch
    ):
        """ツール実行が例外を投げてもループは止まらず、エラー文字列を tool_result として渡す。"""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        with patch("anthropic.Anthropic") as mock_anthropic:
            instance = mock_anthropic.return_value
            instance.messages.create.side_effect = [
                self._make_tool_use_response("fetch_bid_detail", {"url": "x"}),
                self._make_text_response(
                    '{"verdict": "uncertain", "confidence": 30, "reason": "情報不足"}'
                ),
            ]

            executor = MagicMock()
            executor.execute.side_effect = RuntimeError("network down")

            judge = BidJudge(enabled_config_with_tools)
            result = judge.judge(sample_bid, ["データ分析"], executor=executor)

            assert result.verdict == "uncertain"
            assert result.tool_calls == 1


class TestJudgeToolExecutor:
    def test_unknown_tool_returns_error(self):
        from judge.tools import JudgeToolExecutor
        executor = JudgeToolExecutor()
        result = executor.execute("nonexistent_tool", {})
        assert "Error" in result
        assert "nonexistent_tool" in result

    def test_fetch_pdf_link_returns_message(self):
        from judge.tools import JudgeToolExecutor
        executor = JudgeToolExecutor()
        result = executor.execute(
            "fetch_bid_detail", {"url": "https://example.jp/doc.pdf"}
        )
        assert "PDF" in result

    def test_fetch_no_session_returns_error(self):
        from judge.tools import JudgeToolExecutor
        executor = JudgeToolExecutor(http_session=None)
        result = executor.execute(
            "fetch_bid_detail", {"url": "https://example.jp/page"}
        )
        assert "Error" in result
        assert "session" in result.lower()

    def test_search_past_bids_no_db_returns_error(self):
        from judge.tools import JudgeToolExecutor
        executor = JudgeToolExecutor(db_conn=None)
        result = executor.execute(
            "search_past_bids", {"municipality_name": "松前町"}
        )
        assert "Error" in result
        assert "DB" in result

    def test_search_past_bids_with_mock_db(self):
        from judge.tools import JudgeToolExecutor
        mock_conn = MagicMock()
        mock_row = MagicMock()
        mock_row.__iter__ = lambda self: iter(
            [
                ("title", "データ可視化BI構築"),
                ("bid_type", "proposal"),
                ("published_date", "2026-03-15"),
                ("filter_score", 5),
                ("matched_keywords", "データ分析,BI"),
                ("url", "https://example.jp/past1"),
            ]
        )
        mock_row.keys = MagicMock(
            return_value=[
                "title", "bid_type", "published_date",
                "filter_score", "matched_keywords", "url",
            ]
        )
        # Make dict() conversion work
        mock_conn.execute.return_value.fetchall.return_value = [
            {
                "title": "データ可視化BI構築",
                "bid_type": "proposal",
                "published_date": "2026-03-15",
                "filter_score": 5,
                "matched_keywords": "データ分析,BI",
                "url": "https://example.jp/past1",
            }
        ]

        executor = JudgeToolExecutor(db_conn=mock_conn)
        result = executor.execute(
            "search_past_bids", {"municipality_name": "松前町", "limit": 3}
        )
        assert "松前町" in result
        assert "データ可視化BI構築" in result
