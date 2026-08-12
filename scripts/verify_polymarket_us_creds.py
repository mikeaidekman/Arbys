"""Check that Polymarket US API credentials work. Run this first.

    venv\\Scripts\\python.exe scripts/verify_polymarket_us_creds.py

Hits the real authenticated API, so it needs credentials. It diagnoses the
common failures rather than just reporting "401", because every one of them
surfaces as an auth error with no further hint:

  * credentials not configured
  * key file unreadable or not the base64 the portal issued
  * system clock more than 30s from the venue's (signatures are time-bound)
  * identity verification not yet approved

Nothing here is needed for market data - the public gateway
(gateway.polymarket.us) requires no credentials at all.
"""

from __future__ import annotations

import os
import sys
import time
from email.utils import parsedate_to_datetime

import httpx
from dotenv import load_dotenv

from arbys.adapters.polymarket_us_auth import AUTH_BASE, auth_headers, creds_from_env

# The backend loads .env at import; a bare script does not, and without this
# the check reports "not configured" for credentials that are in fact set.
load_dotenv()

# Cheap, read-only, and requires a verified account - a good canary.
PROBE_PATH = "/v1/portfolio/positions"
CLOCK_TOLERANCE_S = 30.0


def _fail(msg: str) -> int:
    print(f"\nFAILED: {msg}")
    return 1


def main() -> int:
    print("Polymarket US credential check")
    print("=" * 60)

    key_id = os.environ.get("POLYMARKET_US_API_KEY_ID")
    key_path = os.environ.get("POLYMARKET_US_PRIVATE_KEY_PATH")
    print(f"POLYMARKET_US_API_KEY_ID      : {'set' if key_id else 'NOT SET'}")
    print(f"POLYMARKET_US_PRIVATE_KEY_PATH: {key_path or 'NOT SET'}")

    if not key_id or not key_path:
        return _fail(
            "credentials not configured.\n\n"
            "  1. Install the Polymarket US app and create an account\n"
            "  2. Complete identity verification (this is the slow step)\n"
            "  3. Visit https://polymarket.us/developer and sign in\n"
            "  4. Generate an API key - the secret is shown ONCE (copy it)\n\n"
            "  Save the secret to a file OUTSIDE this repo, then set:\n"
            "     POLYMARKET_US_API_KEY_ID=<the key id>\n"
            "     POLYMARKET_US_PRIVATE_KEY_PATH=<path to that file>"
        )

    creds = creds_from_env()
    if creds is None:
        return _fail(
            f"the key file at {key_path} could not be loaded.\n"
            "  It should contain the base64 secret exactly as the developer\n"
            "  portal displayed it - one line, nothing else. A trailing\n"
            "  newline is fine. If you no longer have it, generate a new key;\n"
            "  the secret is not recoverable."
        )
    print("key file                      : loaded, valid Ed25519 secret")

    # Clock skew first: signatures are only valid within 30s, and a skewed
    # clock produces a 401 that looks exactly like a bad key.
    try:
        head = httpx.get(f"{AUTH_BASE}{PROBE_PATH}", timeout=20)
        server_date = head.headers.get("date")
        if server_date:
            server_ts = parsedate_to_datetime(server_date).timestamp()
            skew = time.time() - server_ts
            status = "ok" if abs(skew) <= CLOCK_TOLERANCE_S else "TOO FAR"
            print(f"clock skew vs venue           : {skew:+.1f}s ({status})")
            if abs(skew) > CLOCK_TOLERANCE_S:
                return _fail(
                    f"system clock is {skew:+.1f}s from the venue's, outside the\n"
                    f"  {CLOCK_TOLERANCE_S:.0f}s signing tolerance. Every signed request\n"
                    "  will be rejected. Sync your clock and re-run."
                )
    except httpx.HTTPError as exc:
        print(f"clock skew vs venue           : could not check ({exc})")

    print(f"\nprobing {AUTH_BASE}{PROBE_PATH} ...")
    try:
        resp = httpx.get(
            f"{AUTH_BASE}{PROBE_PATH}",
            headers=auth_headers(creds, "GET", PROBE_PATH),
            timeout=30,
        )
    except httpx.HTTPError as exc:
        return _fail(f"could not reach {AUTH_BASE}: {exc}")

    print(f"HTTP {resp.status_code}")

    if resp.status_code == 200:
        print("\nSUCCESS - credentials are valid and the account is approved.")
        print("The WebSocket at wss://api.polymarket.us/v1/ws/markets can now be wired.")
        return 0

    if resp.status_code in (401, 403):
        return _fail(
            f"authentication rejected (HTTP {resp.status_code}).\n"
            f"  body: {resp.text[:300]}\n\n"
            "  Most likely, in order:\n"
            "   - identity verification is not yet approved (the key exists\n"
            "     but cannot trade or read a portfolio until it is)\n"
            "   - the key id and secret are from different key pairs\n"
            "   - the key was revoked or regenerated in the portal"
        )

    return _fail(f"unexpected HTTP {resp.status_code}: {resp.text[:300]}")


if __name__ == "__main__":
    sys.exit(main())
