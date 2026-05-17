"""Outbound URL validation helpers.

The crawler accepts user-provided URLs, so it must reject destinations that
could turn browser fetches into SSRF probes against local or private services.
"""
import ipaddress
import socket
from typing import Iterable
from urllib.parse import urlparse

from src.config.settings import ALLOW_PRIVATE_NETWORK_URLS


SAFE_SCHEMES = {"http", "https"}
LOCAL_HOSTNAMES = {"localhost", "localhost.localdomain"}


class UnsafeURL(ValueError):
    """Raised when a URL is not safe for outbound fetching."""


def validate_fetch_url(url: str, allow_private: bool = ALLOW_PRIVATE_NETWORK_URLS) -> str:
    """Validate and return a URL that is safe for crawler/server-side fetches."""
    parsed = urlparse(url)

    if parsed.scheme.lower() not in SAFE_SCHEMES:
        raise UnsafeURL("Only http and https URLs are allowed")
    if not parsed.hostname:
        raise UnsafeURL("URL must include a host")
    if parsed.username or parsed.password:
        raise UnsafeURL("Credentials in URLs are not allowed")

    hostname = parsed.hostname.rstrip(".").lower()
    if not allow_private:
        if hostname in LOCAL_HOSTNAMES or hostname.endswith(".local"):
            raise UnsafeURL("Local network hostnames are not allowed")
        for address in _resolve_host(hostname):
            if _is_private_or_special(address):
                raise UnsafeURL("Private, local, or reserved network addresses are not allowed")

    return url


def validate_fetch_urls(urls: Iterable[str], allow_private: bool = ALLOW_PRIVATE_NETWORK_URLS) -> list[str]:
    """Validate a collection of URLs for crawler/server-side fetching."""
    return [validate_fetch_url(url, allow_private=allow_private) for url in urls]


def _resolve_host(hostname: str) -> tuple[ipaddress._BaseAddress, ...]:
    try:
        ip = ipaddress.ip_address(hostname)
        return (ip,)
    except ValueError:
        pass

    try:
        addrinfos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeURL(f"Unable to resolve host: {hostname}") from exc

    addresses = []
    for family, _, _, _, sockaddr in addrinfos:
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        addresses.append(ipaddress.ip_address(sockaddr[0]))

    if not addresses:
        raise UnsafeURL(f"Unable to resolve host: {hostname}")

    return tuple(addresses)


def _is_private_or_special(address: ipaddress._BaseAddress) -> bool:
    return any(
        [
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        ]
    )
