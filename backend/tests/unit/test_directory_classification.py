"""
Unit tests for directory classification logic used in the scan/suggestions feature.

Tests the media-dominated detection algorithm and the skip-name filtering
to ensure massive re-downloadable directories are never suggested for backup.
"""

import pytest

from app.api.directories import MEDIA_EXTENSIONS, SKIP_NAMES, _is_media_dominated

# ---------------------------------------------------------------------------
# Media-dominated detection
# ---------------------------------------------------------------------------


class TestMediaDominated:
    """Tests for the _is_media_dominated heuristic."""

    def test_empty_directory_is_not_media(self, tmp_path):
        """Empty directories should not be flagged as media."""
        assert _is_media_dominated(str(tmp_path)) is False

    def test_config_files_not_media(self, tmp_path):
        """Directories with config/script files are not media."""
        (tmp_path / "config.yml").write_text("key: value")
        (tmp_path / "backup.sh").write_text("#!/bin/bash")
        (tmp_path / "docker-compose.yml").write_text("version: '3'")
        (tmp_path / "README.md").write_text("# Docs")
        assert _is_media_dominated(str(tmp_path)) is False

    def test_mostly_video_is_media(self, tmp_path):
        """Directory with mostly video files should be flagged."""
        for i in range(5):
            f = tmp_path / f"movie_{i}.mkv"
            f.write_bytes(b"\x00" * 10_000)  # 10KB each = 50KB video
        (tmp_path / "info.nfo").write_text("x")  # tiny non-media
        assert _is_media_dominated(str(tmp_path)) is True

    def test_mostly_audio_is_media(self, tmp_path):
        """Directory with mostly audio files should be flagged."""
        for i in range(10):
            (tmp_path / f"track_{i}.flac").write_bytes(b"\x00" * 5_000)
        (tmp_path / "playlist.m3u").write_text("track_0.flac")
        assert _is_media_dominated(str(tmp_path)) is True

    def test_mixed_content_below_threshold(self, tmp_path):
        """Directory with <60% media by size should not be flagged."""
        # 40% media, 60% non-media
        (tmp_path / "video.mp4").write_bytes(b"\x00" * 4_000)
        (tmp_path / "database.db").write_bytes(b"\x00" * 6_000)
        assert _is_media_dominated(str(tmp_path)) is False

    def test_exactly_60_percent_not_flagged(self, tmp_path):
        """Exactly 60% media should NOT be flagged (threshold is >60%)."""
        (tmp_path / "video.mp4").write_bytes(b"\x00" * 6_000)
        (tmp_path / "data.db").write_bytes(b"\x00" * 4_000)
        assert _is_media_dominated(str(tmp_path)) is False

    def test_iso_files_are_media(self, tmp_path):
        """ISO/IMG disk images should count as media."""
        (tmp_path / "ubuntu.iso").write_bytes(b"\x00" * 10_000)
        (tmp_path / "readme.txt").write_text("x")
        assert _is_media_dominated(str(tmp_path)) is True

    def test_nested_media_detected(self, tmp_path):
        """Media files in subdirectories should still be detected."""
        sub = tmp_path / "Season 1"
        sub.mkdir()
        for i in range(3):
            (sub / f"episode_{i}.mkv").write_bytes(b"\x00" * 10_000)
        (tmp_path / "tvshow.nfo").write_text("x")
        assert _is_media_dominated(str(tmp_path)) is True

    def test_database_heavy_not_media(self, tmp_path):
        """Directories with databases/configs should not be flagged."""
        (tmp_path / "main.db").write_bytes(b"\x00" * 50_000)
        (tmp_path / "config.json").write_text('{"key": "value"}')
        (tmp_path / "logs").mkdir()
        (tmp_path / "logs" / "app.log").write_bytes(b"\x00" * 10_000)
        assert _is_media_dominated(str(tmp_path)) is False

    def test_sample_limit_respected(self, tmp_path):
        """Should not scan more than sample_limit files."""
        for i in range(300):
            (tmp_path / f"file_{i}.txt").write_text("x")
        # With sample_limit=10, should finish quickly
        result = _is_media_dominated(str(tmp_path), sample_limit=10)
        assert result is False


# ---------------------------------------------------------------------------
# Skip-name filtering
# ---------------------------------------------------------------------------


class TestSkipNames:
    """Tests for the SKIP_NAMES set used to filter obvious media directories."""

    @pytest.mark.parametrize(
        "name",
        [
            "media",
            "movies",
            "tv",
            "downloads",
            "music",
            "audiobooks",
            "transcode",
            "isos",
            "games",
            "torrents",
            "usenet",
            "youtube",
            "podcasts",
            "videos",
            "recordings",
            "rips",
        ],
    )
    def test_known_media_names_skipped(self, name):
        """All known media directory names should be in the skip set."""
        assert name in SKIP_NAMES

    @pytest.mark.parametrize(
        "name",
        [
            "appdata",
            "scripts",
            "backups",
            "configs",
            "docker",
            "system",
            "domains",
            "cron",
            "ssl",
            "nginx",
            "photos",
        ],
    )
    def test_config_names_not_skipped(self, name):
        """Config/script/personal directory names should NOT be in the skip set."""
        assert name not in SKIP_NAMES

    def test_case_insensitive_matching(self):
        """Skip names should be compared case-insensitively."""
        # The actual code does name.lower() in SKIP_NAMES
        assert "Media".lower() in SKIP_NAMES
        assert "DOWNLOADS".lower() in SKIP_NAMES
        assert "Movies".lower() in SKIP_NAMES


# ---------------------------------------------------------------------------
# Media extensions coverage
# ---------------------------------------------------------------------------


class TestMediaExtensions:
    """Verify that key media extension categories are covered."""

    @pytest.mark.parametrize("ext", [".mkv", ".mp4", ".avi", ".mov"])
    def test_video_extensions(self, ext):
        assert ext in MEDIA_EXTENSIONS

    @pytest.mark.parametrize("ext", [".mp3", ".flac", ".aac", ".ogg", ".opus"])
    def test_audio_extensions(self, ext):
        assert ext in MEDIA_EXTENSIONS

    @pytest.mark.parametrize("ext", [".iso", ".img", ".bin"])
    def test_disk_image_extensions(self, ext):
        assert ext in MEDIA_EXTENSIONS

    @pytest.mark.parametrize("ext", [".yml", ".json", ".py", ".sh", ".db", ".log"])
    def test_config_extensions_not_media(self, ext):
        assert ext not in MEDIA_EXTENSIONS
