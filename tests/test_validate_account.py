"""
Tests for the account validation helper.

Only the pure functions are covered — no server, no network, no live account.
That is the whole testable surface worth guarding: the rest of the script is
HTTP plumbing whose behaviour is the API's, already tested elsewhere.

The JSESSIONID normalisation is the reason this tool exists, so it gets the most
attention. Pasting the quoted value straight from DevTools produces a session
that passes whoami and then fails every write — a failure that looks like
success until something silently doesn't send.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# Loaded by path: scripts/ is not a package, and importing it as one would need
# an __init__.py that exists only to satisfy the test.
_SPEC = importlib.util.spec_from_file_location(
    "validate_account",
    Path(__file__).resolve().parents[1] / "scripts" / "validate_account.py",
)
validate_account = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(validate_account)

normalize_jsessionid = validate_account.normalize_jsessionid
validate_li_at = validate_account.validate_li_at
mask = validate_account.mask
summarize = validate_account.summarize


# --- JSESSIONID normalisation ---------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('"ajax:1234567890"', "ajax:1234567890"),   # straight from DevTools
        ("ajax:1234567890", "ajax:1234567890"),     # already bare
        ('  "ajax:123"  ', "ajax:123"),             # padded by the paste
        ("'ajax:123'", "ajax:123"),                 # single quotes
        ("", None),
        (None, None),
        ('""', None),                               # quotes around nothing
    ],
)
def test_jsessionid_is_normalized(raw, expected):
    assert normalize_jsessionid(raw) == expected


def test_inner_quotes_are_preserved():
    """Only a matched surrounding pair is stripped, not quotes within the value."""
    assert normalize_jsessionid('"aj"ax"') == 'aj"ax'


def test_mismatched_quotes_are_left_alone():
    assert normalize_jsessionid("\"ajax:123'") == "\"ajax:123'"


# --- li_at validation ------------------------------------------------------


def test_li_at_rejects_empty():
    assert validate_li_at("") is not None
    assert validate_li_at(None) is not None
    assert validate_li_at("   ") is not None


def test_li_at_rejects_a_truncated_paste():
    error = validate_li_at("AQEDAT")
    assert error is not None
    assert "truncated" in error


def test_li_at_accepts_a_plausible_value():
    assert validate_li_at("A" * 40) is None


# --- Masking ---------------------------------------------------------------


def test_mask_never_reveals_the_secret():
    secret = "AQEDATEEsuperSecretCookieValue1234567890"
    masked = mask(secret)
    assert secret not in masked
    assert secret[8:] not in masked
    assert "40 chars" in masked


def test_mask_of_a_short_value_shows_no_prefix_at_all():
    assert mask("abcd") == "(set, 4 chars)"


def test_mask_of_nothing():
    assert mask(None) == "(none)"
    assert mask("") == "(none)"


# --- Report rendering ------------------------------------------------------


def test_failed_session_reports_not_ok():
    ok, lines = summarize(
        {
            "ok": False,
            "identity": {},
            "summary": "rejected",
            "checks": [
                {
                    "name": "whoami",
                    "label": "Session is valid",
                    "ok": False,
                    "critical": True,
                    "error": "mobile: voyager request failed || playwright: unbound",
                    "impact": "Nothing works until the session is accepted.",
                }
            ],
        }
    )

    assert ok is False
    body = "\n".join(lines)
    # The real Voyager error must reach the operator, not just the fallback's.
    assert "voyager request failed" in body
    assert "Nothing works until" in body


def test_successful_report_shows_identity():
    ok, lines = summarize(
        {
            "ok": True,
            "identity": {
                "display_name": "Dana Example",
                "headline": "Head of Growth",
                "profile_url": "https://www.linkedin.com/in/dana",
            },
            "checks": [{"name": "whoami", "label": "Session is valid", "ok": True}],
        }
    )

    assert ok is True
    body = "\n".join(lines)
    assert "Dana Example" in body
    assert "Head of Growth" in body


def test_inbox_failure_gets_an_explicit_callout():
    """
    A failed inbox probe is singled out.

    It is the one non-critical failure with a behavioural consequence: reply
    detection goes blind, so a sequence keeps following up after someone has
    already answered.
    """
    _, lines = summarize(
        {
            "ok": True,
            "identity": {"display_name": "Dana"},
            "checks": [
                {"name": "whoami", "label": "Session is valid", "ok": True},
                {
                    "name": "fetch_inbox",
                    "label": "Can read conversations",
                    "ok": False,
                    "error": "HTTP 400",
                    "impact": "Replies can't be detected.",
                },
            ],
        }
    )

    body = "\n".join(lines)
    assert "NOTE: fetch_inbox failed" in body
    assert "keep following up" in body


def test_no_callout_when_inbox_is_healthy():
    _, lines = summarize(
        {
            "ok": True,
            "identity": {},
            "checks": [
                {"name": "fetch_inbox", "label": "Can read conversations", "ok": True}
            ],
        }
    )

    assert "NOTE: fetch_inbox failed" not in "\n".join(lines)
