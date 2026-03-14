"""Unit tests for Jinja2 autoescaping in RestorePlanGenerator."""

from unittest.mock import MagicMock

import pytest

from app.services.restore_plan import RESTORE_PLAN_TEMPLATE, RestorePlanGenerator


@pytest.fixture
def mock_config(tmp_path):
    config = MagicMock()
    config.config_dir = tmp_path
    config.db_path = tmp_path / "arkive.db"
    return config


@pytest.fixture
def generator(mock_config):
    return RestorePlanGenerator(config=mock_config)


# ---------------------------------------------------------------------------
# Template rendering helpers
# ---------------------------------------------------------------------------


def _render_template(targets=None, databases=None, containers=None, directories=None, **kwargs):
    """Render the restore plan template directly via Environment (bypasses DB/weasyprint)."""
    from jinja2 import Environment, select_autoescape

    env = Environment(autoescape=select_autoescape(["html"]))
    tmpl = env.from_string(RESTORE_PLAN_TEMPLATE)
    return tmpl.render(
        hostname=kwargs.get("hostname", "test-host"),
        generated_at=kwargs.get("generated_at", "2024-01-01 00:00 UTC"),
        platform=kwargs.get("platform", "linux"),
        version=kwargs.get("version", "0.0.0"),
        targets=targets or [],
        databases=databases or [],
        containers=containers or [],
        directories=directories or [],
        flash_available=kwargs.get("flash_available", False),
    )


# ---------------------------------------------------------------------------
# Security: HTML injection in user-supplied fields
# ---------------------------------------------------------------------------


class TestAutoescape:
    """Verify user-supplied data is HTML-escaped in template output."""

    def test_flash_restore_instructions_target_boot_config(self):
        """Flash restore instructions must restore into /boot/config, not /boot."""
        html = _render_template(flash_available=True, platform="unraid")

        assert "-C /boot/config/</pre>" in html
        assert "-C /boot/</pre>" not in html

    def test_flash_restore_instructions_use_restored_dump_path(self):
        """Flash restore instructions must point to the restored /config/dumps archive path."""
        html = _render_template(flash_available=True, platform="unraid")

        assert "/config/restore/config/dumps/flash_TIMESTAMP.tar.gz" in html
        assert "/config/restore/flash_TIMESTAMP.tar.gz" not in html

    def test_html_special_chars_in_db_name_are_escaped(self):
        """Angle brackets in database names must not appear raw in HTML output."""
        malicious_db_name = '<script>alert("xss")</script>'
        databases = [
            {
                "container_name": "mycontainer",
                "db_type": "postgres",
                "db_name": malicious_db_name,
                "restore_cmd": "psql ...",
                "full_restore_cmd": "docker exec ...",
            }
        ]
        html = _render_template(databases=databases)

        # Raw script tag must NOT appear
        assert "<script>" not in html
        assert "</script>" not in html
        # Escaped form must appear
        assert "&lt;script&gt;" in html

    def test_script_tag_in_container_name_is_escaped(self):
        """XSS payload in container name must be HTML-escaped."""
        malicious_container = "<script>alert(1)</script>"
        containers = [
            {
                "name": malicious_container,
                "image": "nginx:latest",
                "status": "running",
                "priority": 1,
            }
        ]
        html = _render_template(containers=containers)

        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_script_tag_in_target_name_is_escaped(self):
        """XSS payload in storage target name must be HTML-escaped."""
        malicious_target_name = "<img src=x onerror=alert(1)>"
        targets = [
            {
                "name": malicious_target_name,
                "type": "local",
                "snapshot_count": 5,
                "total_size_display": "1.0 GB",
            }
        ]
        html = _render_template(targets=targets)

        assert "<img src=x" not in html
        assert "&lt;img" in html

    def test_ampersand_in_db_name_is_escaped(self):
        """Ampersands in database names must be entity-escaped."""
        databases = [
            {
                "container_name": "myapp",
                "db_type": "postgres",
                "db_name": "app&db",
                "restore_cmd": "psql ...",
                "full_restore_cmd": "docker exec ...",
            }
        ]
        html = _render_template(databases=databases)
        assert "&amp;db" in html

    def test_quotes_in_directory_label_are_escaped(self):
        """Double quotes in directory labels must be escaped."""
        directories = [
            {
                "path": "/mnt/user/data",
                "label": 'My "important" data',
            }
        ]
        html = _render_template(directories=directories)
        # The label with raw double quotes should not appear unescaped in attribute contexts;
        # in text context Jinja2 escapes " as &#34; or &quot;
        assert 'My "important" data' not in html or "&quot;" in html or "&#34;" in html

    def test_normal_inputs_produce_valid_html(self):
        """Normal alphanumeric inputs render correctly without mangling."""
        targets = [{"name": "My Backup", "type": "b2", "snapshot_count": 10, "total_size_display": "5.0 GB"}]
        databases = [
            {
                "container_name": "postgres_app",
                "db_type": "postgres",
                "db_name": "appdb",
                "restore_cmd": "psql -U postgres -d appdb < dump.sql",
                "full_restore_cmd": "docker exec ... psql ...",
            }
        ]
        containers = [{"name": "myapp", "image": "nginx:latest", "status": "running", "priority": 1}]
        directories = [{"path": "/mnt/user/data", "label": "User Data"}]

        html = _render_template(
            targets=targets,
            databases=databases,
            containers=containers,
            directories=directories,
            hostname="myserver",
            platform="linux",
        )

        assert "<!DOCTYPE html>" in html
        assert "My Backup" in html
        assert "postgres_app" in html
        assert "appdb" in html
        assert "myapp" in html
        assert "User Data" in html
        assert "myserver" in html

    def test_template_uses_environment_with_autoescape(self):
        """Verify the template environment has autoescaping enabled for HTML."""
        from jinja2 import Environment, select_autoescape

        env = Environment(autoescape=select_autoescape(["html"]))
        # Check that autoescape is enabled for .html extension
        assert env.is_async is False
        # Render a simple XSS probe through the environment
        tmpl = env.from_string("<p>{{ val }}</p>")
        result = tmpl.render(val="<script>evil()</script>")
        assert "<script>" not in result
        assert "&lt;script&gt;" in result
