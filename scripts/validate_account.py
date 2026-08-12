#!/usr/bin/env python
"""
Connect a LinkedIn account and preflight it, in one command.

This is the tool for validating the Voyager/mobile endpoints against a real
session — the last unproven part of the system. It replaces a sequence of curl
calls, a hand-minted dev token and some JSON piping with:

    python scripts/validate_account.py                      # connect, then preflight
    python scripts/validate_account.py --account-id <id>    # preflight again
    python scripts/validate_account.py --account-id <id> --rotate

Nothing here writes to LinkedIn. Preflight's four probes (whoami, fetch_profile,
fetch_inbox, fetch_activity) only read, so this is safe to run against a
production account at any time.

Why it talks HTTP instead of importing the code
-----------------------------------------------
It drives the running API rather than calling ``run_preflight`` directly, so it
exercises the same path the product does — auth, the Fernet encrypt/decrypt
round-trip, transport selection — and the same command works unchanged against a
deployed instance with ``--api-base``. A helper that bypassed all that could pass
while the real flow was broken.

Handling of the cookies
-----------------------
``li_at`` is a bearer credential: whoever holds it is signed in as that account,
with no password and no second factor. So:

* it is read from the environment or a no-echo prompt, never from argv, because
  argv lands in shell history and process listings;
* it is never printed, logged or written to disk — every code path that shows it
  goes through ``mask``;
* no password is ever requested. (There is a legacy password-login path in
  ``src/agents/core/account_manager_agent.py``; it contradicts the design stated
  in ``src/accounts/schemas.py`` and is deliberately not used here.)
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from typing import Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402

DEFAULT_API_BASE = "http://127.0.0.1:8000"
MIN_LI_AT = 20  # matches AccountConnect.li_at (src/accounts/schemas.py)

# Test-runner convention: the verdict leads, so "FAIL  Session is valid" reads as
# a named check that failed rather than as a claim.
PASS = "PASS"
FAIL = "FAIL"

# The server's report contains em-dashes; a Windows console defaults to cp1252
# and renders them as mojibake, in output an operator is meant to read closely.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested in tests/test_validate_account.py)
# ---------------------------------------------------------------------------


def normalize_jsessionid(raw: Optional[str]) -> Optional[str]:
    """
    Strip whitespace and the quotes LinkedIn stores JSESSIONID with.

    DevTools shows the value as ``"ajax:1234567890"`` — quotes included — and
    pasting it verbatim is the single most expensive mistake available here. The
    transport re-adds the quotes itself when building the cookie header
    (``src/infrastructure/transports/mobile.py``), so a pre-quoted value becomes
    double-quoted. The CSRF header then stops matching the cookie, and the result
    is the worst kind of failure: ``whoami`` still succeeds, so the account looks
    connected, and every *write* fails.
    """
    if raw is None:
        return None
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value or None


def mask(secret: Optional[str]) -> str:
    """Render a credential safely: enough to tell two apart, not enough to use."""
    if not secret:
        return "(none)"
    if len(secret) <= 8:
        return f"(set, {len(secret)} chars)"
    return f"{secret[:4]}...(set, {len(secret)} chars)"


def validate_li_at(value: Optional[str]) -> Optional[str]:
    """Return an error message, or None when the value is plausible."""
    if not value or not value.strip():
        return "li_at is empty. Copy it from DevTools -> Application -> Cookies."
    if len(value.strip()) < MIN_LI_AT:
        return (
            f"li_at is only {len(value.strip())} characters; the API requires at "
            f"least {MIN_LI_AT}. This usually means a truncated copy — the real "
            f"value is long and starts with 'AQEDA'."
        )
    return None


def summarize(report: dict) -> Tuple[bool, list]:
    """
    Turn a preflight report into (session_ok, lines_to_print).

    Kept separate from the printing so the formatting is testable without a
    server or a live account.
    """
    lines: list = []
    checks = report.get("checks") or []

    for check in checks:
        ok = bool(check.get("ok"))
        marker = f"{PASS} " if ok else f"{FAIL} "
        lines.append(f"  {marker}{check.get('label') or check.get('name')}")
        if not ok:
            if check.get("error"):
                lines.append(f"       error : {check['error']}")
            if check.get("impact"):
                lines.append(f"       impact: {check['impact']}")

    identity = report.get("identity") or {}
    if identity.get("display_name") or identity.get("public_id"):
        lines.append("")
        lines.append(f"  Connected as: {identity.get('display_name') or '?'}")
        if identity.get("headline"):
            lines.append(f"  Headline    : {identity['headline']}")
        if identity.get("profile_url"):
            lines.append(f"  Profile     : {identity['profile_url']}")

    # fetch_inbox earns its own callout: without it, reply detection is blind and
    # a sequence will keep following up after someone has answered.
    inbox = next((c for c in checks if c.get("name") == "fetch_inbox"), None)
    if inbox is not None and not inbox.get("ok"):
        lines.append("")
        lines.append(
            "  NOTE: fetch_inbox failed. Replies cannot be detected, so sequences "
            "would keep following up after someone answers. Fix this before any "
            "real outreach."
        )

    return bool(report.get("ok")), lines


# ---------------------------------------------------------------------------
# API plumbing
# ---------------------------------------------------------------------------


def dev_token() -> str:
    """
    Mint a local token for CLERK_DEV_UNSAFE mode.

    That mode skips signature verification but still requires a bearer token to
    be present, so something has to produce one. Local development only — the
    server accepts any signature when it is enabled.
    """
    import jwt

    return jwt.encode(
        {"sub": "validate-script", "email": "local@example.com", "exp": 9999999999},
        # The signature is never checked in this mode, but the key still has to
        # clear PyJWT's minimum length or it warns on stderr and clutters output
        # that operators are meant to read carefully.
        "local-dev-signature-is-not-verified-by-the-server",
        algorithm="HS256",
    )


def resolve_token(explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    if os.getenv("API_TOKEN"):
        return os.environ["API_TOKEN"]
    if os.getenv("CLERK_DEV_UNSAFE", "").lower() == "true":
        return dev_token()
    raise SystemExit(
        "No API token available.\n"
        "  Local:  set CLERK_DEV_UNSAFE=true on the server AND in this shell.\n"
        "  Remote: pass --token <clerk session jwt>, or set API_TOKEN."
    )


def check_health(client: httpx.Client) -> None:
    """Fail early and specifically, rather than during connect."""
    try:
        response = client.get("/healthz", timeout=10)
    except httpx.ConnectError:
        raise SystemExit(
            f"Could not reach the API at {client.base_url}.\n"
            "Is the server running? Start it with:\n"
            "  USE_SQLITE=true CLERK_DEV_UNSAFE=true ENCRYPTION_KEY=<key> \\\n"
            "    python -m uvicorn src.api.main:app --host 127.0.0.1 --port 8000"
        )

    components = (response.json() or {}).get("components", {})

    database = components.get("database", "")
    if database and database != "ok":
        raise SystemExit(f"Database is not ready: {database}")

    encryption = components.get("credentials_encryption", "")
    if "no ENCRYPTION_KEY" in encryption:
        raise SystemExit(
            "The server has no ENCRYPTION_KEY, so it cannot store credentials.\n"
            "Generate one and restart the server with it set:\n"
            "  python -c \"from cryptography.fernet import Fernet; "
            'print(Fernet.generate_key().decode())"'
        )
    if "insecure" in encryption.lower() or "dev" in encryption.lower():
        print(
            "  ! WARNING: the server is using the insecure development encryption\n"
            "    key. A real LinkedIn session cookie would be stored under a key\n"
            "    that is public knowledge. Set ENCRYPTION_KEY before continuing.\n"
        )


def read_cookies() -> Tuple[str, Optional[str]]:
    """Take cookies from the environment, or prompt without echoing them."""
    li_at = os.getenv("LI_AT")
    jsessionid = os.getenv("JSESSIONID")

    if not li_at:
        print("Paste the cookies from DevTools -> Application -> Cookies ->")
        print("https://www.linkedin.com  (input is hidden)\n")
        li_at = getpass.getpass("  li_at                : ")
    if jsessionid is None:
        jsessionid = getpass.getpass("  JSESSIONID (optional): ")

    error = validate_li_at(li_at)
    if error:
        raise SystemExit(f"  {FAIL}  {error}")

    li_at = li_at.strip()
    jsessionid = normalize_jsessionid(jsessionid)

    print(f"\n  li_at      : {mask(li_at)}")
    print(f"  JSESSIONID : {mask(jsessionid)}")
    if not jsessionid:
        print(
            "  ! No JSESSIONID. Reads will work; every write will fail, because\n"
            "    LinkedIn's CSRF check compares a header against that cookie."
        )
    print(
        "  ! Both cookies must come from the SAME browser session. Mixing them\n"
        "    produces a session that passes whoami and fails everything else.\n"
    )
    return li_at, jsessionid


def _raise_for_api_error(response: httpx.Response, what: str) -> None:
    if response.status_code < 400:
        return
    try:
        detail = response.json().get("detail", response.text)
    except Exception:
        detail = response.text
    if response.status_code == 401:
        detail = (
            f"{detail}\nThe server rejected the token. Make sure CLERK_DEV_UNSAFE=true "
            "is set on the SERVER process, not just in this shell."
        )
    raise SystemExit(f"  {FAIL}  {what} failed (HTTP {response.status_code}): {detail}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Connect a LinkedIn account and run the read-only preflight.",
        epilog="Cookies are read from LI_AT / JSESSIONID or prompted for; never pass "
        "them as arguments, which would put a live credential in your shell history.",
    )
    parser.add_argument("--api-base", default=os.getenv("API_BASE", DEFAULT_API_BASE))
    parser.add_argument("--token", help="Bearer token (default: minted for dev mode)")
    parser.add_argument(
        "--account-id",
        help="Preflight this existing account instead of connecting a new one. "
        "Use this while iterating on endpoint shapes.",
    )
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="With --account-id: replace the stored cookies first (for expiry).",
    )
    parser.add_argument("--label", default="validation", help="Friendly name")
    parser.add_argument(
        "--mode", default="outreach", choices=["outreach", "account_based_engagement"]
    )
    parser.add_argument("--json", action="store_true", help="Print the raw report too")
    args = parser.parse_args()

    if args.rotate and not args.account_id:
        raise SystemExit("--rotate needs --account-id (there is nothing to rotate yet).")

    token = resolve_token(args.token)
    client = httpx.Client(
        base_url=args.api_base.rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )

    with client:
        print(f"\n== Checking the API at {args.api_base}")
        check_health(client)
        print(f"  {PASS}  API is up and can store credentials\n")

        account_id = args.account_id

        if account_id and not args.rotate:
            print(f"== Reusing account {account_id} (no new account created)\n")
        else:
            li_at, jsessionid = read_cookies()
            payload = {"li_at": li_at, "jsessionid": jsessionid}

            if args.rotate:
                print(f"== Rotating credentials on {account_id}")
                response = client.post(
                    f"/api/v1/accounts/{account_id}/credentials", json=payload
                )
                _raise_for_api_error(response, "Credential rotation")
            else:
                print("== Connecting the account")
                response = client.post(
                    "/api/v1/accounts",
                    json={**payload, "label": args.label, "mode": args.mode},
                )
                _raise_for_api_error(response, "Connect")
                account_id = response.json()["id"]

            status = response.json().get("status")
            if status == "active":
                print(f"  {PASS}  LinkedIn accepted the session (status: active)")
            else:
                # Not fatal: preflight below reports exactly which probe failed
                # and why, which is more useful than stopping here.
                print(f"  {FAIL}  LinkedIn rejected the session (status: {status})")
            print(f"  account id: {account_id}\n")

        print("== Preflight (read-only: nothing is liked, sent, connected or posted)")
        response = client.post(f"/api/v1/accounts/{account_id}/preflight")
        _raise_for_api_error(response, "Preflight")
        report = response.json()

    ok, lines = summarize(report)
    print()
    for line in lines:
        print(line)

    print()
    print(f"  {report.get('summary', '')}")
    for step in report.get("next_steps") or []:
        print(f"    - {step}")

    if args.json:
        print("\n== Raw report")
        print(json.dumps(report, indent=2))

    print()
    if ok:
        print("Session is valid. Re-run any time with:")
        print(f"     python scripts/validate_account.py --account-id {account_id}")
    else:
        print("Session is NOT usable. Fix the cookies, then:")
        print(
            f"     python scripts/validate_account.py --account-id {account_id} --rotate"
        )
    print()

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
