"""账号模型请求仅连接已校验的公网 HTTPS 地址，并固定本次 DNS 结果。"""

import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit

import httpx


class ProviderEndpointError(ValueError):
    """可直接展示的地址错误，不包含用户输入或凭据。"""


def _public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if not address.is_global:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped and not address.ipv4_mapped.is_global:
            return False
        if address.sixtofour or address.teredo:
            return False
    return True


def normalize_base_url(value: str) -> str:
    message = "接口地址必须是公网 HTTPS 地址，不能包含账号、查询参数或片段"
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ProviderEndpointError(message)
    value = value.strip()
    if any(ord(char) <= 32 or ord(char) == 127 for char in value) or "\\" in value:
        raise ProviderEndpointError(message)
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        port = parts.port or 443
        if (
            parts.scheme != "https"
            or not host
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError()
        if not 1 <= port <= 65535 or "%" in host:
            raise ValueError()
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if "." not in host or not all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in host.split(".")
            ):
                raise ValueError() from None
        else:
            if not _public_ip(str(address)):
                raise ValueError()
        authority = f"[{host}]" if ":" in host else host
        if port != 443:
            authority += f":{port}"
        return urlunsplit(("https", authority, parts.path.rstrip("/"), "", ""))
    except (ValueError, UnicodeError):
        raise ProviderEndpointError(message) from None


def _resolve_public(host: str, port: int) -> str:
    try:
        addresses = list(
            dict.fromkeys(
                row[4][0]
                for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            )
        )
    except (OSError, UnicodeError):
        raise ProviderEndpointError("无法解析模型服务地址，请检查域名") from None
    if not addresses or any(not _public_ip(address) for address in addresses):
        raise ProviderEndpointError("模型服务必须解析到公网地址，不能访问本机或内网")
    # 部署网络优先使用 IPv4；所有返回地址都先检查，不能忽略混入的私网记录。
    return sorted(addresses, key=lambda address: ":" in address)[0]


class ProviderTransport(httpx.BaseTransport):
    def __init__(self, base_url: str):
        self.base_url = httpx.URL(normalize_base_url(base_url))
        self._inner = httpx.HTTPTransport(trust_env=False, retries=0)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if (request.url.scheme, request.url.host, request.url.port or 443) != (
            "https",
            self.base_url.host,
            self.base_url.port or 443,
        ):
            raise ProviderEndpointError("模型请求地址与已保存的服务商不一致")
        address = _resolve_public(self.base_url.host, self.base_url.port or 443)
        headers = httpx.Headers(request.headers)
        headers["Host"] = self.base_url.netloc.decode("ascii")
        # IP 用于实际连接；原域名用于 Host、TLS SNI 和证书校验，避免二次 DNS 解析。
        pinned = httpx.Request(
            request.method,
            request.url.copy_with(host=address),
            headers=headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": self.base_url.host},
        )
        response = self._inner.handle_request(pinned)
        if 300 <= response.status_code < 400:
            response.close()
            raise ProviderEndpointError(
                "模型接口发生地址跳转，请填写最终的公网 HTTPS API 地址"
            )
        return response

    def close(self) -> None:
        self._inner.close()


def build_provider_http_client(base_url: str) -> httpx.Client:
    return httpx.Client(
        transport=ProviderTransport(base_url),
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(60.0, connect=10.0),
    )
