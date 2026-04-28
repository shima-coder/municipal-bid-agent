"""Slack Webhook通知"""

import json
import logging
from datetime import datetime

import requests

from scraper.base import load_config

logger = logging.getLogger(__name__)

# 種別の表示名マッピング
BID_TYPE_LABELS = {
    'proposal': 'プロポーザル',
    'bid': '入札',
    'negotiation': '随意契約',
    'unknown': '不明',
}

# AI判定 verdict の表示名
VERDICT_LABELS = {
    'apply': '✅ 応募推奨',
    'skip': '❌ スキップ推奨',
    'uncertain': '❔ 要確認',
}


class SlackNotifier:
    """Slack Incoming Webhookで通知を送信する"""

    def __init__(self, config=None):
        if config is None:
            config = load_config()
        notification = config.get('notification', {})
        self.webhook_url = notification.get('slack_webhook_url', '')

    @property
    def is_configured(self):
        """Slack webhook URLが設定されているか"""
        return bool(self.webhook_url)

    def format_bid_message(self, bid, score, matched_keywords, judgment=None):
        """個別案件の通知メッセージを生成する。

        Args:
            bid: dict with keys title, municipality_name, prefecture,
                 published_date, bid_type, url, matched_keywords
            score: int
            matched_keywords: list[str]
            judgment: BidJudgment (optional) — LLM judgement to attach

        Returns:
            str: formatted message
        """
        title = bid.get('title', '不明')
        municipality_name = bid.get('municipality_name') or bid.get('name', '不明')
        prefecture = bid.get('prefecture', '')
        published_date = bid.get('published_date') or '不明'
        bid_type = BID_TYPE_LABELS.get(bid.get('bid_type', 'unknown'), '不明')
        url = bid.get('url', '')
        keywords_str = ', '.join(matched_keywords) if matched_keywords else 'なし'

        lines = [
            f"🔔 新着案件: {score}点",
            "",
            f"📋 {title}",
            f"🏛️ {municipality_name}（{prefecture}）",
            f"📅 公告日: {published_date}",
            f"🏷️ 種別: {bid_type}",
            f"🔑 キーワード: {keywords_str}",
            f"🔗 {url}",
        ]

        if judgment is not None and not judgment.is_empty:
            verdict_label = VERDICT_LABELS.get(judgment.verdict, '❔')
            lines.append("")
            lines.append(
                f"🤖 AI判定: {verdict_label}（信頼度 {judgment.confidence}%）"
            )
            if judgment.reason:
                lines.append(f"   理由: {judgment.reason}")
            if judgment.estimated_effort and judgment.estimated_effort != '不明':
                lines.append(f"   想定工数: {judgment.estimated_effort}")
            if judgment.concerns:
                lines.append(f"   懸念: {' / '.join(judgment.concerns)}")

        return '\n'.join(lines)

    def format_summary_message(self, stats):
        """日次サマリメッセージを生成する。

        Args:
            stats: dict with keys:
                - total_municipalities: int
                - success_count: int
                - failure_count: int
                - total_new_items: int
                - notify_count: int

        Returns:
            str: formatted summary message
        """
        now = datetime.now().strftime('%Y-%m-%d %H:%M')
        total = stats.get('total_municipalities', 0) or 0
        success = stats.get('success_count', 0) or 0
        failure = stats.get('failure_count', 0) or 0
        new_items = stats.get('total_new_items', 0) or 0
        notify_count = stats.get('notify_count', 0) or 0

        lines = [
            f"📊 スクレイピング完了 ({now})",
            "",
            f"対象自治体: {total}件",
            f"取得成功: {success}件 / 失敗: {failure}件",
            f"新規案件: {new_items}件（うち通知対象: {notify_count}件）",
        ]
        return '\n'.join(lines)

    def send(self, message):
        """メッセージを送信する。webhook未設定時はコンソール出力。

        Args:
            message: str

        Returns:
            bool: True if sent successfully (or printed to console)
        """
        if not self.is_configured:
            print(message)
            return True

        try:
            response = requests.post(
                self.webhook_url,
                data=json.dumps({'text': message}),
                headers={'Content-Type': 'application/json'},
                timeout=10,
            )
            if response.status_code == 200:
                logger.info("Slack notification sent successfully")
                return True
            else:
                logger.error(
                    f"Slack notification failed: {response.status_code} {response.text}"
                )
                return False
        except requests.RequestException as e:
            logger.error(f"Slack notification error: {e}")
            return False

    def notify_bid(self, bid, score, matched_keywords, judgment=None):
        """個別案件を通知する"""
        message = self.format_bid_message(bid, score, matched_keywords, judgment)
        return self.send(message)

    def notify_summary(self, stats):
        """日次サマリを通知する"""
        message = self.format_summary_message(stats)
        return self.send(message)
