from core.local_helper_release import get_local_helper_release


def test_release_config_only_exposes_valid_https_packages(monkeypatch):
    monkeypatch.setattr("config.LOCAL_WECHAT_HELPER_ENABLED", True)
    monkeypatch.setattr("config.LOCAL_WECHAT_HELPER_VERSION", "0.1.0-beta.1")
    monkeypatch.setattr("config.LOCAL_WECHAT_HELPER_MIN_API_VERSION", 1)
    monkeypatch.setattr(
        "config.LOCAL_WECHAT_HELPER_ARM64_URL",
        "https://download.example.com/helper-arm64.dmg",
    )
    monkeypatch.setattr("config.LOCAL_WECHAT_HELPER_ARM64_SHA256", "ab" * 32)
    monkeypatch.setattr(
        "config.LOCAL_WECHAT_HELPER_X64_URL", "http://unsafe.example/helper.dmg"
    )
    monkeypatch.setattr("config.LOCAL_WECHAT_HELPER_X64_SHA256", "cd" * 32)

    release = get_local_helper_release()

    assert release["enabled"] is True
    assert release["release_channel"] == "unsigned-open-source-beta"
    assert release["min_api_version"] == 1
    assert release["supported_wechat_versions"] == ["4.1.12"]
    assert release["downloads"] == [
        {
            "architecture": "arm64",
            "url": "https://download.example.com/helper-arm64.dmg",
            "sha256": "ab" * 32,
        }
    ]


def test_release_is_disabled_without_verifiable_package(monkeypatch):
    monkeypatch.setattr("config.LOCAL_WECHAT_HELPER_ENABLED", True)
    monkeypatch.setattr(
        "config.LOCAL_WECHAT_HELPER_ARM64_URL",
        "https://download.example.com/helper.dmg",
    )
    monkeypatch.setattr("config.LOCAL_WECHAT_HELPER_ARM64_SHA256", "invalid")
    monkeypatch.setattr("config.LOCAL_WECHAT_HELPER_X64_URL", "")
    monkeypatch.setattr("config.LOCAL_WECHAT_HELPER_X64_SHA256", "")

    assert get_local_helper_release()["enabled"] is False
