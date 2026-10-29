"""HTTP connection layer (SAPConnection / SAPResponse / SAPIOException parity).

Differences vs. the official Java client:
- httpx instead of Apache HttpClient; PEM client certificates instead of PKCS#12
  (PEM).
- The web UI itself serves plain HTTP (internal), but the outbound connection to
  the SAP portal remains HTTPS - enforced here like in the original.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from sapdlm.client.endpoints import EndpointBuilder, EndpointStyle, detect_style

DEFAULT_TIMEOUT = 0.0  # original: DEFAULT_TIMEOUT = 0 (no timeout)
HTTP_RETRY_ATTEMPTS = 3  # original: retry handler max executionCount 3


class SapIOError(Exception):
    """Original: SAPIOException - communication with the SAP portal failed."""


class InvalidProtocolError(SapIOError):
    """Original: InvalidProtocolException - non-https SAP URL."""


class AuthenticationConfigError(SapIOError):
    """Original: org.apache.http.auth.AuthenticationException mapping."""


@dataclass
class ConnectionConfig:
    """Mirror of DownloadClient.setConnectionData arguments."""

    sap_url: str
    username: str = ""
    password: str = ""
    use_cert_auth: bool = False
    cert_path: str = ""  # PEM client certificate (file path)
    cert_key_password: str = ""  # kept for parity; PEM key has no password
    use_proxy: bool = False
    proxy_host: str = ""
    proxy_port: int = 0
    proxy_username: str = ""
    proxy_password: str = ""

    def validate_auth(self) -> None:
        """Mirror of DownloadClient.intPrepareConnectionObject auth checks."""
        if self.use_cert_auth:
            if not self.cert_path.strip() or not self.cert_key_password.strip():
                raise AuthenticationConfigError(
                    "Certificate is not configured in the DLM (HTTP/1.1 401)"
                )
        elif not self.username.strip() or not self.password.strip():
            raise AuthenticationConfigError("Basic is not configured in the DLM (HTTP/1.1 401)")

    def effective_url(self) -> str:
        url = self.sap_url.strip()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        return url


@dataclass
class SapResponse:
    status_code: int
    headers: httpx.Headers
    body: bytes
    request_url: str = ""
    effective_host: str = ""

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def content_length(self) -> int:
        """Mirror of SAPResponse.getContentLength: prefer Content-Range total."""
        length = self.headers.get("Content-Length", "0")
        content_range = self.headers.get("Content-Range", "")
        if content_range:
            pos = content_range.rfind("/")
            if pos > 0:
                try:
                    return int(content_range[pos + 1 :])
                except ValueError:
                    pass
        try:
            return int(length)
        except ValueError:
            return 0


class RefreshCancelled(Exception):
    """A basket refresh was canceled by the user (DownloadClient
    .interruptGetBasketContent parity)."""


class SapConnection:
    """Stateless HTTP executor for portal requests (SAPConnection.getResponse parity)."""

    def __init__(self, config: ConnectionConfig, client: httpx.Client | None = None):
        self.config = config
        self.style: EndpointStyle = detect_style(config.sap_url)
        self.endpoints = EndpointBuilder(self.style)
        self._client = client

    # ------------------------------------------------------------------ client setup

    def _build_client(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        timeout = httpx.Timeout(30.0, read=None)  # generous default; see M2
        proxy = None
        if self.config.use_proxy and self.config.proxy_host:
            proxy_auth = (
                f"{self.config.proxy_username}:{self.config.proxy_password}"
                if self.config.proxy_username
                else None
            )
            proxy = (
                f"http://{proxy_auth + '@' if proxy_auth else ''}"
                f"{self.config.proxy_host}:{self.config.proxy_port}"
            )
        kwargs: dict = {"timeout": timeout, "proxy": proxy, "follow_redirects": True}
        if self.config.use_cert_auth:
            kwargs["cert"] = self.config.cert_path
        else:
            kwargs["auth"] = (self.config.username, self.config.password)
        return httpx.Client(**kwargs)

    # ------------------------------------------------------------------- public API

    def get(self, path: str, headers: dict[str, str] | None = None) -> SapResponse:
        """Single GET. Raises SapIOError like the original's SAPIOException."""
        url = self.config.effective_url().rstrip("/") + path
        if not url.startswith("https://"):
            raise InvalidProtocolError(
                f"SAP Support Portal address protocol is unknown. 'https' is "
                f"required: {self.config.sap_url}"
            )
        client = self._build_client()
        last_error: Exception | None = None
        attempts = HTTP_RETRY_ATTEMPTS
        for _ in range(attempts):
            try:
                response = client.get(url, headers=headers or {})
            except httpx.HTTPError as exc:
                last_error = exc
                continue
            if response.status_code in (401, 403):
                # Surface SAP's own diagnostic (e.g. PWD_WRONG) - the portal
                # reports why credentials were rejected.
                code = response.headers.get("X-message-code", "")
                raise SapIOError(
                    f"Request failed. Exception: HTTP/1.1 {response.status_code}"
                    + (f" ({code})" if code else "")
                )
            if response.status_code >= 400:
                raise SapIOError(f"Request failed. Exception: HTTP/1.1 {response.status_code}")
            return SapResponse(
                status_code=response.status_code,
                headers=response.headers,
                body=response.content,
                request_url=str(response.request.url),
                effective_host=str(response.request.url.host),
            )
        raise SapIOError(f"Request failed. Exception: {last_error}; Class: {type(last_error)}")

    def get_with_retries(self, path: str, attempts: int = 5, stop=None) -> SapResponse:
        """Basket-content fetch: retry up to N times, abort immediately on
        401/403 (DownloadClient.GetBasketContentThread parity). `stop` is a
        threading.Event checked between attempts - canceling a refresh."""
        last_exc: Exception | None = None
        for _ in range(attempts):
            if stop is not None and stop.is_set():
                raise RefreshCancelled()
            try:
                return self.get(path)
            except SapIOError as exc:
                if "HTTP/1.1 401" in str(exc) or "HTTP/1.1 403" in str(exc):
                    raise
                last_exc = exc
                time.sleep(0.5)
        raise SapIOError(f"Unable to read the basket content: {last_exc}")

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
