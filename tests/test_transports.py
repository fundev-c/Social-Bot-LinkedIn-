"""
Tests for the transport scaffold: fingerprint stability, composite routing
(mobile -> Playwright fallback), and the Playwright executor adapter.
"""

import types

import pytest

from src.infrastructure.api_client import CompositeTransport, get_transport
from src.infrastructure.transports.base import (
    LinkedInTransport,
    TransportChallenge,
    TransportResult,
    TransportUnavailable,
)
from src.infrastructure.transports.fingerprints import generate_fingerprint
from src.infrastructure.transports.mobile import MobileAPITransport
from src.infrastructure.transports.playwright import PlaywrightTransport

# asyncio_mode=auto (pytest.ini) auto-marks async tests; no module-wide mark so
# the sync fingerprint tests aren't flagged.


def _account(account_id="acct-1"):
    return types.SimpleNamespace(
        id=account_id, auth_blob=None, device_fingerprint=None, proxy=None
    )


# --- Fingerprints ---

def test_fingerprint_is_stable_per_account():
    a = generate_fingerprint("acct-123")
    b = generate_fingerprint("acct-123")
    assert a == b
    assert generate_fingerprint("acct-999") != a


def test_fingerprint_has_required_fields():
    fp = generate_fingerprint("acct-123")
    for key in ("platform", "user_agent", "device_id", "tls_impersonate", "app_version"):
        assert fp.get(key)
    assert fp["platform"] in ("android", "ios")


def test_forced_platform():
    assert generate_fingerprint("x", platform="ios")["platform"] == "ios"
    assert generate_fingerprint("x", platform="android")["platform"] == "android"


# --- Fake transports for routing tests ---

class FakeTransport:
    def __init__(self, name, behavior="ok"):
        self.name = name
        self.behavior = behavior
        self.calls = []

    async def like(self, account, activity_urn):
        self.calls.append("like")
        if self.behavior == "unavailable":
            raise TransportUnavailable(f"{self.name} unavailable")
        if self.behavior == "challenge":
            raise TransportChallenge(f"{self.name} challenge")
        if self.behavior == "fail":
            return TransportResult(success=False, action="like", via=self.name, error="failed")
        return TransportResult(success=True, action="like", via=self.name)


async def test_primary_success_skips_fallback():
    primary = FakeTransport("mobile", "ok")
    fallback = FakeTransport("playwright", "ok")
    comp = CompositeTransport(primary, fallback)

    result = await comp.like(_account(), "urn:activity:1")
    assert result.success
    assert result.via == "mobile"
    assert fallback.calls == []  # fallback not invoked


async def test_unavailable_triggers_fallback():
    primary = FakeTransport("mobile", "unavailable")
    fallback = FakeTransport("playwright", "ok")
    comp = CompositeTransport(primary, fallback)

    result = await comp.like(_account(), "urn:activity:1")
    assert result.success
    assert result.via == "playwright"
    assert result.detail["fell_back_from"] == "mobile"
    assert fallback.calls == ["like"]


async def test_challenge_triggers_fallback():
    comp = CompositeTransport(FakeTransport("mobile", "challenge"), FakeTransport("playwright", "ok"))
    result = await comp.like(_account(), "urn:activity:1")
    assert result.success and result.via == "playwright"


async def test_both_unavailable_returns_failure():
    comp = CompositeTransport(
        FakeTransport("mobile", "unavailable"), FakeTransport("playwright", "unavailable")
    )
    result = await comp.like(_account(), "urn:activity:1")
    assert not result.success
    assert result.error


async def test_no_fallback_returns_failure_on_unavailable():
    comp = CompositeTransport(FakeTransport("mobile", "unavailable"), fallback=None)
    result = await comp.like(_account(), "urn:activity:1")
    assert not result.success


async def test_both_failing_reports_the_primary_error_too():
    """
    When both transports fail, the primary's error must survive.

    Until the Playwright executor is bound its failure is always the same
    uninformative sentence. If that were the only thing reported, it would mask
    the Voyager response — which is the one piece of information needed to fix a
    drifted endpoint shape against a live account.
    """
    comp = CompositeTransport(
        FakeTransport("mobile", "unavailable"),
        FakeTransport("playwright", "unavailable"),
    )

    result = await comp.like(_account(), "urn:activity:1")

    assert not result.success
    assert "mobile unavailable" in result.error
    assert "playwright unavailable" in result.error
    assert result.detail["primary_error"] == "mobile unavailable"
    assert result.detail["primary_via"] == "mobile"
    assert result.detail["fallback_error"] == "playwright unavailable"


# --- Mobile scaffold falls back today ---

async def test_mobile_scaffold_signals_unavailable():
    mobile = MobileAPITransport(session_factory=lambda acct: object())
    with pytest.raises(TransportUnavailable):
        await mobile.like(_account(), "urn:activity:1")


# --- Playwright executor adapter ---

class FakeExecutor:
    async def like(self, account, activity_urn):
        return TransportResult(success=True, action="like")


async def test_playwright_delegates_to_executor():
    pw = PlaywrightTransport(executor=FakeExecutor())
    result = await pw.like(_account(), "urn:activity:1")
    assert result.success and result.via == "playwright"


async def test_playwright_without_executor_is_unavailable():
    pw = PlaywrightTransport(executor=None)
    with pytest.raises(TransportUnavailable):
        await pw.like(_account(), "urn:activity:1")


# --- Factory ---

async def test_get_transport_composite_by_default(monkeypatch):
    monkeypatch.delenv("MOBILE_TRANSPORT_ENABLED", raising=False)
    t = get_transport(_account())
    assert isinstance(t, CompositeTransport)


async def test_get_transport_playwright_only_when_disabled(monkeypatch):
    monkeypatch.setenv("MOBILE_TRANSPORT_ENABLED", "false")
    t = get_transport(_account(), playwright_executor=FakeExecutor())
    assert isinstance(t, PlaywrightTransport)
    assert isinstance(t, LinkedInTransport)
