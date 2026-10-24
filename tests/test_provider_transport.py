import socket

import httpx
import pytest

from core.provider_transport import (
    ProviderEndpointError,
    ProviderTransport,
    normalize_base_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com/v1",
        "https://localhost/v1",
        "https://127.0.0.1/v1",
        "https://[::1]/v1",
        "https://[::ffff:127.0.0.1]/v1",
        "https://169.254.169.254/v1",
        "https://10.0.0.1/v1",
        "https://user:secret@api.example.com/v1",
        "https://api.example.com/v1?key=secret",
        "https://api.example.com/v1#secret",
        "https://api.example.com\\@127.0.0.1/v1",
        "https://api.example.com/\nprivate",
        "https://service.internal/v1",
    ],
)
def test_private_or_ambiguous_urls_are_rejected_without_echo(url):
    with pytest.raises(ProviderEndpointError) as error:
        normalize_base_url(url)
    assert url not in str(error.value) and "secret" not in str(error.value)


def test_normalize_public_endpoint():
    assert (
        normalize_base_url("https://API.Example.com:443/v1/")
        == "https://api.example.com/v1"
    )


def records(*addresses):
    return [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
        for address in addresses
    ]


def test_connection_is_pinned_with_original_host_and_tls_name(monkeypatch):
    calls = []
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *args, **kwargs: records("1.1.1.1")
    )
    transport = ProviderTransport("https://api.example.com/v1")
    transport._inner.close()
    transport._inner = httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200, json={"ok": True})
    )
    with httpx.Client(transport=transport, trust_env=False) as client:
        client.post(
            "https://api.example.com/v1/embeddings",
            headers={"Authorization": "Bearer synthetic"},
            json={"input": "fixture"},
        )
    assert len(calls) == 1
    assert calls[0].url.host == "1.1.1.1"
    assert calls[0].headers["Host"] == "api.example.com"
    assert calls[0].extensions["sni_hostname"] == "api.example.com"
    assert calls[0].headers["Authorization"] == "Bearer synthetic"


@pytest.mark.parametrize(
    "addresses", [("1.1.1.1", "10.0.0.2"), ("169.254.169.254",), ("127.0.0.1",)]
)
def test_dns_private_answers_never_reach_transport(monkeypatch, addresses):
    calls = []
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *args, **kwargs: records(*addresses)
    )
    transport = ProviderTransport("https://api.example.com/v1")
    transport._inner.close()
    transport._inner = httpx.MockTransport(
        lambda request: calls.append(request) or httpx.Response(200)
    )
    with httpx.Client(transport=transport, trust_env=False) as client:
        with pytest.raises(ProviderEndpointError):
            client.post("https://api.example.com/v1/embeddings")
    assert calls == []


def test_dns_rebinding_is_checked_again_and_redirects_are_never_followed(monkeypatch):
    addresses = iter([records("1.1.1.1"), records("127.0.0.1")])
    calls = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: next(addresses))
    transport = ProviderTransport("https://api.example.com/v1")
    transport._inner.close()
    transport._inner = httpx.MockTransport(
        lambda request: (
            calls.append(request)
            or httpx.Response(302, headers={"Location": "http://169.254.169.254/"})
        )
    )
    with httpx.Client(
        transport=transport, follow_redirects=True, trust_env=False
    ) as client:
        with pytest.raises(ProviderEndpointError, match="跳转"):
            client.get("https://api.example.com/v1/models")
        with pytest.raises(ProviderEndpointError, match="公网"):
            client.get("https://api.example.com/v1/models")
    assert len(calls) == 1


def test_other_origin_is_rejected_before_dns(monkeypatch):
    def resolve(*args, **kwargs):
        pytest.fail("不同地址不得进入 DNS 阶段")

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    transport = ProviderTransport("https://api.example.com/v1")
    with httpx.Client(transport=transport, trust_env=False) as client:
        with pytest.raises(ProviderEndpointError):
            client.get("https://other.example.com/v1/models")
