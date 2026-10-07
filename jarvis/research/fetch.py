"""
FRIDAY research fetcher — downloads ONE operator-supplied URL, safely.

Phase 5B:
- https:// only, port 443 only, no credentials in the URL
- The host is resolved first; if ANY address is not public (loopback,
  private LAN, link-local incl. cloud metadata 169.254.169.254, multicast,
  reserved, carrier-grade NAT...) the fetch is refused before connecting
- After connecting, the actual peer address is checked again before a
  single byte of the body is read (defends against DNS rebinding)
- Redirects are followed manually, at most 3, each re-validated
- Content-Type allowlist: HTML/XHTML, plain text, Markdown, PDF, EPUB
- Size cap (Content-Length and while streaming) and connect/read timeouts
- No proxies, no cookies, no auth, no JavaScript: a plain GET with a
  descriptive User-Agent
- This is not a crawler: it fetches exactly the page it is given
- Identifies itself with a contact URL in the User-Agent, as sites such
  as Wikipedia require of automated clients (JARVIS_FETCH_CONTACT)

The body is written to a caller-supplied path; the library ingests it.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit

import httpx

FETCHER_VERSION = "5B.2"
MAX_FETCH_BYTES = 25 * 1024 * 1024
MAX_REDIRECTS = 3
DEFAULT_CONTACT = "https://github.com/davidwrightitconsult-svg"


def user_agent() -> str:
    """Descriptive UA with contact info (URL or email) — required by e.g.
    Wikimedia's User-Agent policy; set JARVIS_FETCH_CONTACT to change it."""
    contact = os.getenv("JARVIS_FETCH_CONTACT", "").strip() or DEFAULT_CONTACT
    if not re.fullmatch(r"[A-Za-z0-9@:/._+~-]{3,200}", contact):
        contact = DEFAULT_CONTACT
    return (f"FRIDAY-research/{FETCHER_VERSION} (+{contact}; personal research tool, "
            f"single-page fetch) python-httpx/{httpx.__version__}")
CONTENT_TYPES = {
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "application/pdf": ".pdf",
    "application/epub+zip": ".epub",
}


class FetchError(ValueError):
    """The URL was refused or could not be fetched (reason is safe to show)."""


@dataclass
class Fetched:
    url: str              # final URL after redirects
    content_type: str
    suffix: str
    size: int
    path: Path


Resolver = Callable[[str], list[str]]


def _system_resolver(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise FetchError(f"cannot resolve host {host!r}") from None
    return sorted({info[4][0] for info in infos})


def is_public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def check_url(url: str) -> tuple[str, str]:
    """Static checks. Returns (normalised url, host)."""
    url = str(url or "").strip()
    if len(url) > 2000:
        raise FetchError("URL is too long")
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        raise FetchError("only https:// URLs are allowed")
    if parts.username or parts.password:
        raise FetchError("URLs with embedded credentials are not allowed")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise FetchError("URL has no host")
    try:
        port = parts.port
    except ValueError:
        raise FetchError("URL has an invalid port") from None
    if port not in (None, 443):
        raise FetchError("only the standard HTTPS port (443) is allowed")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        raise FetchError(f"host {host!r} is not a public internet address")
    return parts._replace(fragment="").geturl(), host


class SafeFetcher:
    def __init__(
        self,
        max_bytes: int = MAX_FETCH_BYTES,
        timeout: float = 20.0,
        resolver: Resolver = _system_resolver,
        transport: httpx.BaseTransport | None = None,
        verify_peer: bool = True,
    ):
        self.max_bytes = int(max_bytes)
        self.timeout = httpx.Timeout(timeout, connect=10.0)
        self._resolve = resolver
        self._transport = transport
        self._verify_peer = verify_peer   # False only with an injected test transport

    def _check_host(self, host: str) -> None:
        try:
            ipaddress.ip_address(host)
            addresses = [host]            # literal IP in the URL
        except ValueError:
            addresses = self._resolve(host)
        if not addresses:
            raise FetchError(f"cannot resolve host {host!r}")
        bad = [a for a in addresses if not is_public_address(a)]
        if bad:
            raise FetchError(f"host {host!r} resolves to a non-public address; refused")

    def _check_peer(self, response: httpx.Response) -> None:
        if not self._verify_peer:
            return
        stream = response.extensions.get("network_stream")
        peer = stream.get_extra_info("server_addr") if stream is not None else None
        if not peer or not is_public_address(str(peer[0])):
            raise FetchError("connected to a non-public address; refused")

    def fetch(self, url: str, dest_dir: str | Path) -> Fetched:
        current, host = check_url(url)
        dest_dir = Path(dest_dir)
        with httpx.Client(transport=self._transport, timeout=self.timeout, trust_env=False,
                          follow_redirects=False, headers={"User-Agent": user_agent(),
                                                           "Accept": ", ".join(CONTENT_TYPES)}) as client:
            for hop in range(MAX_REDIRECTS + 1):
                self._check_host(host)
                try:
                    with client.stream("GET", current) as response:
                        self._check_peer(response)
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get("location")
                            if not location:
                                raise FetchError("redirect without a location")
                            if hop == MAX_REDIRECTS:
                                raise FetchError(f"more than {MAX_REDIRECTS} redirects")
                            current, host = check_url(urljoin(current, location))
                            continue
                        if response.status_code in (401, 403, 429):
                            raise FetchError(f"server answered HTTP {response.status_code} "
                                             "(the site refused or rate-limited automated access)")
                        if response.status_code != 200:
                            raise FetchError(f"server answered HTTP {response.status_code}")
                        return self._save(response, current, dest_dir)
                except httpx.TimeoutException:
                    raise FetchError("the server took too long to respond") from None
                except httpx.HTTPError as exc:
                    raise FetchError(f"network error: {type(exc).__name__}") from None
        raise FetchError("too many redirects")  # pragma: no cover

    def _save(self, response: httpx.Response, url: str, dest_dir: Path) -> Fetched:
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        suffix = CONTENT_TYPES.get(content_type)
        if suffix is None:
            raise FetchError(f"content type {content_type or '(none)'} is not supported")
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self.max_bytes:
            raise FetchError(f"document larger than {self.max_bytes // (1024 * 1024)} MB")
        name = (Path(urlsplit(url).path).stem or urlsplit(url).hostname or "page")[:80]
        safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in name).strip("-.") or "page"
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"{safe}{suffix}"
        size = 0
        with path.open("wb") as out:
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > self.max_bytes:
                    out.close()
                    path.unlink(missing_ok=True)
                    raise FetchError(f"document larger than {self.max_bytes // (1024 * 1024)} MB")
                out.write(chunk)
        return Fetched(url=url, content_type=content_type, suffix=suffix, size=size, path=path)
