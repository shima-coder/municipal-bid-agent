"""tests/test_notify.py — Slack通知テスト"""

import json
from unittest.mock import patch, MagicMock

import pytest

from notify.slack import SlackNotifier, BID_TYPE_LABELS


# --- fixtures ---

@pytest.fixture
def notifier_no_webhook():
    """webhook未設定のNotifier"""
    config = {'notification': {'slack_webhook_url': ''}}
    return SlackNotifier(config=config)


@pytest.fixture
def notifier_with_webhook():
    """webhook設定済みのNotifier"""
    config = {'notification': {'slack_webhook_url': 'https://hooks.slack.com/services/T00/B00/xxx'}}
    return SlackNotifier(config=config)


@pytest.fixture
def sample_bid():
    return {
        'title': 'データ分析業務委託',
        'municipality_name': '松前町',
        'prefecture': '愛媛県',
        'published_date': '2026-02-20',
        'bid_type': 'proposal',
        'url': 'https://www.town.masaki.ehime.jp/bid/001',
    }


@pytest.fixture
def sample_stats():
    return {
        'total_municipalities': 52,
        'success_count': 50,
        'failure_count': 2,
        'total_new_items': 15,
        'notify_count': 3,
    }


# --- TestSlackNotifierInit ---

class TestSlackNotifierInit:
    def test_init_with_webhook(self, notifier_with_webhook):
        assert notifier_with_webhook.is_configured is True
        assert 'hooks.slack.com' in notifier_with_webhook.webhook_url

    def test_init_without_webhook(self, notifier_no_webhook):
        assert notifier_no_webhook.is_configured is False
        assert notifier_no_webhook.webhook_url == ''

    def test_init_no_notification_section(self):
        notifier = SlackNotifier(config={})
        assert notifier.is_configured is False

    def test_init_none_webhook(self):
        config = {'notification': {'slack_webhook_url': None}}
        notifier = SlackNotifier(config=config)
        assert notifier.is_configured is False


# --- TestFormatBidMessage ---

class TestFormatBidMessage:
    def test_basic_format(self, notifier_no_webhook, sample_bid):
        msg = notifier_no_webhook.format_bid_message(
            sample_bid, score=5, matched_keywords=['データ分析', 'プロポーザル(種別)']
        )
        assert '🔔 新着案件: 5点' in msg
        assert '📋 データ分析業務委託' in msg
        assert '🏛️ 松前町（愛媛県）' in msg
        assert '📅 公告日: 2026-02-20' in msg
        assert '🏷️ 種別: プロポーザル' in msg
        assert '🔑 キーワード: データ分析, プロポーザル(種別)' in msg
        assert '🔗 https://www.town.masaki.ehime.jp/bid/001' in msg

    def test_unknown_bid_type(self, notifier_no_webhook):
        bid = {'title': 'テスト', 'bid_type': 'unknown', 'url': 'http://example.com'}
        msg = notifier_no_webhook.format_bid_message(bid, score=1, matched_keywords=[])
        assert '種別: 不明' in msg

    def test_missing_fields(self, notifier_no_webhook):
        bid = {}
        msg = notifier_no_webhook.format_bid_message(bid, score=0, matched_keywords=[])
        assert '📋 不明' in msg
        assert '公告日: 不明' in msg
        assert 'キーワード: なし' in msg

    def test_bid_type_labels(self, notifier_no_webhook):
        for bid_type, label in BID_TYPE_LABELS.items():
            bid = {'title': 'テスト', 'bid_type': bid_type, 'url': 'http://example.com'}
            msg = notifier_no_webhook.format_bid_message(bid, score=1, matched_keywords=[])
            assert f'種別: {label}' in msg

    def test_name_fallback(self, notifier_no_webhook):
        """municipality_nameがない場合、nameにフォールバック"""
        bid = {'title': 'テスト', 'name': '三好町', 'prefecture': '徳島県', 'url': 'http://example.com'}
        msg = notifier_no_webhook.format_bid_message(bid, score=1, matched_keywords=[])
        assert '三好町' in msg


# --- TestFormatSummaryMessage ---

class TestFormatSummaryMessage:
    def test_basic_format(self, notifier_no_webhook, sample_stats):
        msg = notifier_no_webhook.format_summary_message(sample_stats)
        assert '📊 スクレイピング完了' in msg
        assert '対象自治体: 52件' in msg
        assert '取得成功: 50件 / 失敗: 2件' in msg
        assert '新規案件: 15件（うち通知対象: 3件）' in msg

    def test_zero_stats(self, notifier_no_webhook):
        stats = {
            'total_municipalities': 0,
            'success_count': 0,
            'failure_count': 0,
            'total_new_items': 0,
            'notify_count': 0,
        }
        msg = notifier_no_webhook.format_summary_message(stats)
        assert '対象自治体: 0件' in msg
        assert '取得成功: 0件 / 失敗: 0件' in msg

    def test_none_values(self, notifier_no_webhook):
        stats = {
            'total_municipalities': None,
            'success_count': None,
            'failure_count': None,
            'total_new_items': None,
            'notify_count': None,
        }
        msg = notifier_no_webhook.format_summary_message(stats)
        assert '対象自治体: 0件' in msg

    def test_empty_stats(self, notifier_no_webhook):
        msg = notifier_no_webhook.format_summary_message({})
        assert '対象自治体: 0件' in msg


# --- TestSend ---

class TestSend:
    def test_send_without_webhook_prints(self, notifier_no_webhook, capsys):
        result = notifier_no_webhook.send("テストメッセージ")
        assert result is True
        captured = capsys.readouterr()
        assert 'テストメッセージ' in captured.out

    @patch('notify.slack.requests.post')
    def test_send_with_webhook_success(self, mock_post, notifier_with_webhook):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        result = notifier_with_webhook.send("テスト")
        assert result is True
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[0][0] == notifier_with_webhook.webhook_url
        payload = json.loads(call_args[1]['data'])
        assert payload['text'] == 'テスト'

    @patch('notify.slack.requests.post')
    def test_send_with_webhook_failure(self, mock_post, notifier_with_webhook):
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = 'Internal Server Error'
        mock_post.return_value = mock_response

        result = notifier_with_webhook.send("テスト")
        assert result is False

    @patch('notify.slack.requests.post')
    def test_send_with_request_exception(self, mock_post, notifier_with_webhook):
        import requests
        mock_post.side_effect = requests.ConnectionError("Connection refused")

        result = notifier_with_webhook.send("テスト")
        assert result is False


# --- TestNotifyBid ---

class TestNotifyBid:
    def test_notify_bid_console(self, notifier_no_webhook, sample_bid, capsys):
        result = notifier_no_webhook.notify_bid(
            sample_bid, score=5, matched_keywords=['データ分析']
        )
        assert result is True
        captured = capsys.readouterr()
        assert 'データ分析業務委託' in captured.out
        assert '5点' in captured.out

    @patch('notify.slack.requests.post')
    def test_notify_bid_slack(self, mock_post, notifier_with_webhook, sample_bid):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        result = notifier_with_webhook.notify_bid(
            sample_bid, score=5, matched_keywords=['データ分析']
        )
        assert result is True
        payload = json.loads(mock_post.call_args[1]['data'])
        assert 'データ分析業務委託' in payload['text']


# --- TestNotifySummary ---

class TestNotifySummary:
    def test_notify_summary_console(self, notifier_no_webhook, sample_stats, capsys):
        result = notifier_no_webhook.notify_summary(sample_stats)
        assert result is True
        captured = capsys.readouterr()
        assert 'スクレイピング完了' in captured.out
        assert '対象自治体: 52件' in captured.out

    @patch('notify.slack.requests.post')
    def test_notify_summary_slack(self, mock_post, notifier_with_webhook, sample_stats):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        result = notifier_with_webhook.notify_summary(sample_stats)
        assert result is True
        payload = json.loads(mock_post.call_args[1]['data'])
        assert '対象自治体: 52件' in payload['text']
