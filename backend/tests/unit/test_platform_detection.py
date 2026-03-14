from app.core.platform import Platform, _looks_like_unraid_flash, detect_platform


def test_detect_platform_prefers_unraid_version_file(monkeypatch, tmp_path):
    unraid_version = tmp_path / "unraid-version"
    unraid_version.write_text("6.12.0")
    monkeypatch.setattr("app.core.platform.os.path.exists", lambda path: path == "/etc/unraid-version")
    monkeypatch.setenv("ARKIVE_BOOT_CONFIG_PATH", str(tmp_path / "missing-boot"))

    assert detect_platform() == Platform.UNRAID


def test_detect_platform_recognizes_unraid_flash_mount(monkeypatch, tmp_path):
    boot = tmp_path / "boot-config"
    config_dir = boot / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "super.dat").write_text("x")

    monkeypatch.setenv("ARKIVE_BOOT_CONFIG_PATH", str(boot))
    monkeypatch.setattr("app.core.platform.os.path.exists", lambda path: path == "/etc/os-release")

    assert detect_platform() == Platform.UNRAID


def test_detect_platform_recognizes_direct_config_mount(monkeypatch, tmp_path):
    boot = tmp_path / "boot-config"
    boot.mkdir()
    (boot / "go").write_text("#!/bin/sh")

    monkeypatch.setenv("ARKIVE_BOOT_CONFIG_PATH", str(boot))
    monkeypatch.setattr("app.core.platform.os.path.exists", lambda path: path == "/etc/os-release")

    assert detect_platform() == Platform.UNRAID


def test_detect_platform_falls_back_to_linux_without_unraid_markers(monkeypatch, tmp_path):
    boot = tmp_path / "boot-config"
    boot.mkdir()

    monkeypatch.setenv("ARKIVE_BOOT_CONFIG_PATH", str(boot))
    monkeypatch.setattr(
        "app.core.platform.os.path.exists",
        lambda path: path == "/etc/os-release",
    )

    assert detect_platform() == Platform.LINUX


def test_looks_like_unraid_flash_accepts_known_markers(tmp_path):
    boot = tmp_path / "boot-config"
    config_dir = boot / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "go").write_text("#!/bin/sh")

    assert _looks_like_unraid_flash(boot) is True


def test_looks_like_unraid_flash_accepts_direct_config_mount(tmp_path):
    boot = tmp_path / "boot-config"
    boot.mkdir()
    (boot / "super.dat").write_text("x")

    assert _looks_like_unraid_flash(boot) is True


def test_looks_like_unraid_flash_rejects_plain_directory(tmp_path):
    boot = tmp_path / "boot-config"
    boot.mkdir()

    assert _looks_like_unraid_flash(boot) is False
