from fastapi.testclient import TestClient


def test_proxy_runtime_shell_injects_base_path_and_auth_mode(monkeypatch):
    monkeypatch.setattr("config.PUBLIC_BASE_PATH", "/ex-memory-runtime")
    monkeypatch.setattr("config.METEOR_STORE_SSO_ENABLED", True)
    monkeypatch.setattr("config.METEOR_STORE_PROXY_TOKEN", "proxy-secret")

    from server.app import create_app

    response = TestClient(create_app()).get("/")
    assert response.status_code == 200
    assert 'data-base-path="/ex-memory-runtime"' in response.text
    assert 'data-auth-mode="proxy"' in response.text
    assert 'href="static/style.css' in response.text
    assert 'src="static/app.js' in response.text
    assert 'id="offline-banner"' not in response.text


def test_browser_client_derives_api_and_static_urls_from_runtime_base_path():
    source = "web/static/app.js"
    text = __import__("pathlib").Path(source).read_text(encoding="utf-8")

    assert "document.documentElement.dataset.basePath" in text
    assert "const API = `${BASE_PATH}/api`;" in text
    assert "const PROXY_AUTH" in text
    assert "if (PROXY_AUTH)" in text


def test_proxy_mode_fails_startup_without_shared_token(monkeypatch):
    import pytest

    monkeypatch.setattr("config.METEOR_STORE_SSO_ENABLED", True)
    monkeypatch.setattr("config.METEOR_STORE_PROXY_TOKEN", "")
    from server.app import create_app

    with pytest.raises(RuntimeError, match="METEOR_STORE_PROXY_TOKEN"):
        create_app()
