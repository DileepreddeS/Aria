"""``NAVIGATE(url)`` (SECURITY.md §4, §9).

The browser agent reads hostile pages, so where it may go is decided here rather
than by whatever the page suggests. A URL passes only if all of this holds:

* HTTPS, no credentials in the URL, port 443;
* the host is the posting's ATS host or an allowlisted SSO/asset host for it;
* every address the host resolves to is globally routable.

The last one is the SSRF rule. ``is_global`` is used rather than a list of private
ranges, so loopback, private, link-local, carrier-grade NAT, documentation,
multicast, reserved space and the cloud metadata address at 169.254.169.254 are all
refused by one check, including over IPv6, instead of by a list someone has to keep
complete.

The resolver is injected so the rule stays a pure function in tests. Resolving here
is only half of the defence: the same addresses have to be the ones connected to,
which is the egress proxy's job (SECURITY.md §9, Phase 3).
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlsplit

from aria_core.schemas.policy import PolicyDecision, PolicyRequest, ReasonCode

__all__ = ["Resolver", "check", "host_is_allowed"]

Resolver = Callable[[str], list[str]]

_ALLOWED_SCHEMES = ("https",)
_ALLOWED_PORTS = (443,)


def _system_resolver(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return []
    return [str(info[4][0]) for info in infos]


def host_is_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    """Exact host match, or a suffix match for entries written as ``.example.com``.

    A bare entry never matches a subdomain: allowlisting ``acme.com`` must not
    quietly allow ``evil.acme.com.attacker.test`` or even ``login.acme.com``, which
    is a different host with different content.
    """
    host = host.lower().rstrip(".")
    for entry in allowed:
        candidate = entry.lower().rstrip(".")
        if candidate.startswith("."):
            if host.endswith(candidate) or host == candidate[1:]:
                return True
        elif host == candidate:
            return True
    return False


def check(request: PolicyRequest, *, resolve: Resolver | None = None) -> PolicyDecision:
    from aria_core.policy.engine import allow, deny

    resolve = resolve or _system_resolver
    raw = request.arguments.get("url", "")
    if not raw:
        return deny(request, ReasonCode.MALFORMED_ARGUMENT, "NAVIGATE needs a url argument")

    try:
        parts = urlsplit(raw)
    except ValueError:
        return deny(request, ReasonCode.MALFORMED_ARGUMENT, "the url could not be parsed")

    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        return deny(
            request,
            ReasonCode.SCHEME_NOT_ALLOWED,
            f"only {', '.join(_ALLOWED_SCHEMES)} may be navigated, not {parts.scheme!r}",
        )
    if parts.username or parts.password:
        return deny(
            request,
            ReasonCode.CREDENTIALS_IN_URL,
            "the url carries credentials; secrets are injected by the broker, never in a url",
            "scheme",
        )

    host = parts.hostname
    if not host:
        return deny(request, ReasonCode.MALFORMED_ARGUMENT, "the url has no host", "scheme")

    try:
        port = parts.port or 443
    except ValueError:
        return deny(request, ReasonCode.PORT_NOT_ALLOWED, "the url has an invalid port", "scheme")
    if port not in _ALLOWED_PORTS:
        return deny(request, ReasonCode.PORT_NOT_ALLOWED, f"port {port} is not allowed", "scheme")

    if not host_is_allowed(host, request.context.allowed_hosts):
        return deny(
            request,
            ReasonCode.HOST_NOT_ALLOWED,
            f"{host} is not among this task's allowed hosts",
            "scheme",
            "no_credentials",
            "port",
        )

    passed = ("scheme", "no_credentials", "port", "host_allowlisted")
    addresses = _addresses_for(host, resolve)
    if not addresses:
        return deny(request, ReasonCode.UNRESOLVABLE_HOST, f"{host} did not resolve to any address", *passed)

    for address in addresses:
        if not address.is_global:
            return deny(
                request,
                ReasonCode.PRIVATE_ADDRESS,
                f"{host} resolves to {address}, which is not globally routable",
                *passed,
            )

    return allow(request, *passed, "addresses_global")


def _addresses_for(host: str, resolve: Resolver) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """The addresses to validate: the literal itself, or what the name resolves to."""
    try:
        return [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        pass

    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for candidate in resolve(host):
        try:
            addresses.append(ipaddress.ip_address(candidate.split("%")[0]))
        except ValueError:
            continue
    return addresses
