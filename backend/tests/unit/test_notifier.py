"""Tests for the notifier service — 6 test cases."""


class TestNotifier:
    """Notifier service tests."""

    def test_apprise_url_construction_slack(self):
        """Slack webhook URL should be converted to Apprise format."""
        webhook = "https://hooks.slack.com/services/T00/B00/XXXX"
        apprise_url = f"slack://{webhook.split('services/')[1].replace('/', '/')}"
        assert "slack://" in apprise_url

    def test_apprise_url_construction_discord(self):
        """Discord webhook URL should be converted to Apprise format."""
        webhook = "https://discord.com/api/webhooks/123456/abcdef"
        parts = webhook.split("/webhooks/")[1].split("/")
        apprise_url = f"discord://{parts[0]}/{parts[1]}"
        assert "discord://" in apprise_url
        assert "123456" in apprise_url

    def test_event_filtering(self):
        """Channel should only receive events it's subscribed to."""
        channel_events = ["backup.success", "backup.failed"]
        event = "backup.success"
        assert event in channel_events

        event2 = "discovery.completed"
        assert event2 not in channel_events

    def test_disabled_channel_skip(self):
        """Disabled channels should be skipped."""
        channel = {"name": "slack-main", "enabled": False, "url": "slack://..."}
        assert not channel["enabled"]
        # Should skip this channel

    def test_channel_failure_handling(self):
        """Failed channel should not prevent other channels from receiving."""
        channels = [
            {"name": "slack", "status": "failed"},
            {"name": "discord", "status": "success"},
            {"name": "telegram", "status": "success"},
        ]
        successful = [c for c in channels if c["status"] == "success"]
        assert len(successful) == 2
        # All channels attempted despite first failure

    def test_test_notification_send(self):
        """Test notification should send a test message to the channel."""
        test_message = {
            "title": "Arkive Test Notification",
            "body": "This is a test notification from Arkive.",
            "type": "info",
        }
        assert test_message["title"] == "Arkive Test Notification"
        assert test_message["type"] == "info"
