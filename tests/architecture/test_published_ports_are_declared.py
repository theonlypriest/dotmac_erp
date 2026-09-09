"""Every published socket has a declared owner, and 8888 is forbidden.

Containment success is "the listener is gone and the external path is closed".
DURABLE success is "the controller refuses to recreate it". This module is the
second half.

On 2026-08-30 a root-owned `python3 -m http.server 8888 --bind 0.0.0.0`, started
from an interactive agent session on 2026-03-01 and serving
`/root/.agent/diagrams`, was found listening on the production host with a UFW
`ALLOW IN Anywhere` rule on both address families. It had run for 182 days. No
systemd unit, timer, cron entry or supervisor owned it, which is precisely why
nothing ever flagged it: there was no declared roster to be absent from.

These tests make the roster explicit, so an undeclared publish fails the build
rather than living for six months.

Scope and honesty about it: this checks what the REPOSITORY declares. It cannot
see a process someone starts by hand on the host -- that gap is Observer's, and
is tracked as the runtime half in docs/runbooks/retire-latent-writers.md. Do not
read a green run here as "the host has no undeclared listener".
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "docker-compose.yml"
PRODUCT_TOML = REPO_ROOT / "deploy" / "product.toml"
RENDERED_COMPOSE = REPO_ROOT / "deploy" / "rendered" / "docker-compose.yml"

#: Host ports this product is allowed to publish, each with a named owner.
#: Adding an entry here is a deliberate, reviewable act. Anything not listed
#: fails, which is the property that makes the roster worth having.
AUTHORIZED_HOST_PORTS: dict[int, str] = {
    8003: "app -- HTTP, loopback only, both address families, fronted by nginx",
    6382: "redis -- Celery broker, loopback only",
}

#: Ports that must never appear, with the reason. A port that once carried an
#: incident earns a permanent named refusal rather than silent absence: absence
#: is indistinguishable from nobody having looked.
#:
#: 8002 is deliberately NOT here. Its problem was never the number -- it is the
#: app's own container port and the rendered bundle publishes it on loopback
#: quite legitimately. What went wrong was app-dev publishing it on EVERY
#: interface in short syntax. Listing it as forbidden and then carving out two
#: exceptions would read as incoherent to the next person. The interface rule
#: below is the invariant that actually encodes that defect.
FORBIDDEN_HOST_PORTS: dict[int, str] = {
    8888: (
        "unowned root http.server serving /root/.agent/diagrams, publicly "
        "exposed for 182 days; contained 2026-08-30"
    ),
}

#: The only host addresses a publish may bind. The 2026-08-30 findings were all
#: one shape: a socket reachable from somewhere it had no business being. Short
#: syntax with no host_ip binds 0.0.0.0 AND [::], and on this fleet the IPv6 half
#: cannot be filtered with DOCKER-USER at all -- an inbound v6 connection is
#: accepted by docker-proxy as a local process and traverses INPUT, while
#: DOCKER-USER is only ever jumped from FORWARD. Binding the listener is the
#: fix; filtering it afterwards is not.
LOOPBACK_HOST_IPS = {"127.0.0.1", "::1"}


def _published_host_ports(compose_text: str) -> dict[int, str]:
    """Map host port -> service, for every publish in a compose document.

    Handles both syntaxes deliberately. Short syntax (``"8888:8888"``) is how
    the 8002 exposure happened: it publishes on 0.0.0.0 AND [::] with no host_ip,
    and it is easy to add without noticing. A checker that only understood long
    syntax would miss exactly the shape that caused the problem.
    """
    document = yaml.safe_load(compose_text) or {}
    found: dict[int, str] = {}
    for service, spec in (document.get("services") or {}).items():
        for entry in (spec or {}).get("ports") or []:
            if isinstance(entry, dict):
                published = entry.get("published")
                if published is not None:
                    found[int(published)] = service
                continue
            # Short syntax: [host_ip:]host[:container][/proto]
            text = str(entry).split("/", 1)[0]
            parts = text.split(":")
            if len(parts) == 1:
                continue  # container-only, publishes an ephemeral host port
            found[int(parts[-2])] = service
    return found


def test_every_published_port_has_a_declared_owner() -> None:
    published = _published_host_ports(COMPOSE.read_text(encoding="utf-8"))
    undeclared = {
        port: service
        for port, service in published.items()
        if port not in AUTHORIZED_HOST_PORTS
    }
    assert not undeclared, (
        f"undeclared published port(s): {undeclared}. Add an entry to "
        "AUTHORIZED_HOST_PORTS naming the owner, or stop publishing it. An "
        "unowned socket is how the 8888 exposure survived 182 days."
    )


def test_forbidden_ports_are_not_published() -> None:
    published = _published_host_ports(COMPOSE.read_text(encoding="utf-8"))
    for port, reason in FORBIDDEN_HOST_PORTS.items():
        assert port not in published, f"port {port} must never be published: {reason}"


def test_the_two_rosters_cannot_both_claim_a_port() -> None:
    """A port declared authorized AND forbidden would make the guard incoherent."""
    overlap = set(AUTHORIZED_HOST_PORTS) & set(FORBIDDEN_HOST_PORTS)
    assert not overlap, f"ports both authorized and forbidden: {sorted(overlap)}"


def test_the_rendered_bundle_publishes_only_declared_ports() -> None:
    """The generated deployment must not widen exposure beyond the descriptor."""
    if not RENDERED_COMPOSE.exists():
        return
    published = _published_host_ports(RENDERED_COMPOSE.read_text(encoding="utf-8"))
    declared = set(AUTHORIZED_HOST_PORTS)
    # The rendered bundle publishes the app on 8002 rather than 8003. That is a
    # deliberate descriptor-declared difference, not a widening -- both are
    # loopback, and the interface rule below is what actually polices exposure.
    declared.add(8002)
    stray = {p: s for p, s in published.items() if p not in declared}
    assert not stray, f"rendered bundle publishes undeclared port(s): {stray}"


def test_the_descriptor_declares_the_ingress_port_it_routes_to() -> None:
    descriptor = tomllib.loads(PRODUCT_TOML.read_text(encoding="utf-8"))
    routes = descriptor.get("ingress", {}).get("routes", [])
    assert routes, "the descriptor must declare at least one ingress route"
    for route in routes:
        assert "port" in route, f"ingress route without a port: {route}"
        assert route["port"] not in FORBIDDEN_HOST_PORTS, (
            f"ingress route targets a forbidden port: {route}"
        )


# ---------------------------------------------------------------------------
# Sensitivity. A guard that cannot fail is decorative.
# ---------------------------------------------------------------------------


def test_planting_an_undeclared_listener_is_detected() -> None:
    """Plant the exact shape that caused the incident and require detection.

    Both syntaxes are planted, because the historical exposure used the short
    form and a checker blind to it would pass this file while missing the real
    thing.
    """
    short_syntax = """
services:
  app:
    image: example
    ports:
    - "8888:8888"
"""
    long_syntax = """
services:
  app:
    image: example
    ports:
    - target: 8888
      published: "8888"
      host_ip: 0.0.0.0
      protocol: tcp
"""
    for label, document in (("short", short_syntax), ("long", long_syntax)):
        published = _published_host_ports(document)
        assert 8888 in published, f"{label} syntax publish was not detected at all"
        undeclared = set(published) - set(AUTHORIZED_HOST_PORTS)
        assert 8888 in undeclared, (
            f"{label} syntax: a planted 8888 publish was not flagged as "
            "undeclared -- the roster check has stopped detecting anything"
        )


def test_the_detector_does_not_flag_a_legitimate_publish() -> None:
    """The converse: an authorized port must NOT be reported, or the guard is
    noise and will be disabled by the first person it inconveniences."""
    document = """
services:
  app:
    image: example
    ports:
    - target: 8002
      published: "8003"
      host_ip: 127.0.0.1
      protocol: tcp
"""
    published = _published_host_ports(document)
    assert published == {8003: "app"}, published
    assert not set(published) - set(AUTHORIZED_HOST_PORTS)


def _non_loopback_publishes(compose_text: str) -> dict[str, list]:
    """Publishes that bind something other than a loopback address.

    A short-syntax entry with no host_ip is the dangerous case and is reported,
    because omitting the address is what binds every interface in both families.
    """
    document = yaml.safe_load(compose_text) or {}
    offenders: dict[str, list] = {}
    for service, spec in (document.get("services") or {}).items():
        for entry in (spec or {}).get("ports") or []:
            if isinstance(entry, dict):
                host_ip = entry.get("host_ip")
            else:
                text = str(entry).split("/", 1)[0]
                parts = text.split(":")
                host_ip = ":".join(parts[:-2]) if len(parts) > 2 else None
            if len(str(entry).split(":")) == 1 and not isinstance(entry, dict):
                continue  # container-only
            if host_ip not in LOOPBACK_HOST_IPS:
                offenders.setdefault(service, []).append(entry)
    return offenders


def test_every_publish_binds_loopback_explicitly() -> None:
    offenders = _non_loopback_publishes(COMPOSE.read_text(encoding="utf-8"))
    assert not offenders, (
        f"publish(es) not bound to loopback: {offenders}. Name an explicit "
        "host_ip per address family in long syntax. Omitting it binds 0.0.0.0 "
        "AND [::], and the IPv6 half cannot be filtered with DOCKER-USER."
    )


def test_planting_an_all_interfaces_publish_is_detected() -> None:
    """The app-dev shape, planted. Short syntax with no host_ip must be caught."""
    planted = """
services:
  app-dev:
    image: example
    ports:
    - "8002:8002"
"""
    offenders = _non_loopback_publishes(planted)
    assert "app-dev" in offenders, (
        "a short-syntax all-interfaces publish was not flagged -- this is the "
        "exact shape that put a second production-database writer on a public "
        "socket, and the check has stopped detecting it"
    )


def test_the_interface_detector_accepts_an_explicit_loopback_bind() -> None:
    accepted = """
services:
  app:
    image: example
    ports:
    - target: 8002
      published: "8003"
      host_ip: 127.0.0.1
    - target: 8002
      published: "8003"
      host_ip: "::1"
"""
    assert not _non_loopback_publishes(accepted)
