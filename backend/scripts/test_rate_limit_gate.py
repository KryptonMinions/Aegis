"""Rate-limit + passwordless-login gate (no live services needed).

Checks:
  1. SlidingWindowLimiter: blocks past the limit, isolates keys, recovers
     after the window, honours both windows, and 0 disables a limit.
  2. client_ip uses the last X-Forwarded-For entry (the one Cloud Run adds).
  3. POST /ask returns 429 rate_limited + Retry-After once an IP is over limit.
  4. POST /auth/demo-login is 404 when disabled, and mints a role-scoped
     session through the (stubbed) Supabase calls when enabled.

Run: cd backend && python -m scripts.test_rate_limit_gate
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.update({
    "SUPABASE_URL": "https://ci.invalid",
    "SUPABASE_PUBLISHABLE_KEY": "ci-dummy",
    "SUPABASE_SECRET_KEY": "ci-dummy",
    "THREAD_STORE_BACKEND": "memory",
    "PASSWORDLESS_LOGIN_ENABLED": "false",  # pinned: a developer .env may enable it
    "ASK_RATE_LIMIT_PER_MINUTE": "3",
    "ASK_RATE_LIMIT_PER_HOUR": "0",
    "LOGIN_RATE_LIMIT_PER_MINUTE": "2",
})

from starlette.requests import Request  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
import app.routers.auth as authmod  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.rate_limit import SlidingWindowLimiter, client_ip  # noqa: E402

passed = failed = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


print("=== SlidingWindowLimiter ===")
now = [0.0]
lim = SlidingWindowLimiter(clock=lambda: now[0])
check("first 3 of 3 allowed", all(lim.hit("a", [(60, 3)]) is None for _ in range(3)))
retry = lim.hit("a", [(60, 3)])
check("4th blocked with retry-after", retry is not None and 1 <= retry <= 60, str(retry))
check("other key unaffected", lim.hit("b", [(60, 3)]) is None)
now[0] = 61.0
check("allowed again after the window", lim.hit("a", [(60, 3)]) is None)
lim2 = SlidingWindowLimiter(clock=lambda: now[0])
check("hourly limit applies on its own", all(lim2.hit("k", [(60, 100), (3600, 2)]) is None for _ in range(2)))
check("hourly limit blocks", lim2.hit("k", [(60, 100), (3600, 2)]) is not None)
check("max 0 disables", all(lim2.hit("z", [(60, 0)]) is None for _ in range(50)))

print("\n=== client_ip ===")


def _req(headers: dict[str, str], peer: str = "10.0.0.1") -> Request:
    return Request({
        "type": "http", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": (peer, 1234),
    })


check("last forwarded entry wins (spoofed prefix ignored)",
      client_ip(_req({"x-forwarded-for": "1.1.1.1, 203.0.113.9"})) == "203.0.113.9")
check("falls back to socket peer", client_ip(_req({})) == "10.0.0.1")

print("\n=== /ask returns 429 when over limit ===")
client = TestClient(m.app)
codes = [client.post("/ask", json={}, headers={"x-forwarded-for": "198.51.100.7"}).status_code for _ in range(4)]
check("first 3 reach auth (401), 4th is 429", codes == [401, 401, 401, 429], str(codes))
r = client.post("/ask", json={}, headers={"x-forwarded-for": "198.51.100.7"})
check("429 body uses the error envelope", r.json().get("error", {}).get("code") == "rate_limited", r.text)
check("429 carries Retry-After", int(r.headers.get("retry-after", "0")) >= 1)
check("different IP is not limited",
      client.post("/ask", json={}, headers={"x-forwarded-for": "198.51.100.8"}).status_code == 401)

print("\n=== /auth/demo-login ===")
check("404 when disabled", client.post("/auth/demo-login", json={"role": "analyst"}).status_code == 404)

os.environ["PASSWORDLESS_LOGIN_ENABLED"] = "true"
get_settings.cache_clear()


async def _fake_lookup(username, settings):
    return f"{username}@ksp.internal"


async def _fake_session(email, settings):
    role = "analyst" if email.startswith("analyst") else "admin"
    return {"access_token": "tok", "refresh_token": "ref", "expires_in": 3600, "token_type": "bearer",
            "user": {"id": "u-1", "app_metadata": {"role": role}}}


authmod.lookup_synthetic_email = _fake_lookup
authmod.passwordless_session = _fake_session
hdr = {"x-forwarded-for": "198.51.100.50"}
r = client.post("/auth/demo-login", json={"role": "analyst"}, headers=hdr)
check("enabled: returns a session with the role from the server", r.status_code == 200 and r.json()["user"]["role"] == "analyst", r.text)
check("unknown role rejected", client.post("/auth/demo-login", json={"role": "root"}, headers=hdr).status_code == 422)
r = client.post("/auth/demo-login", json={"role": "admin"}, headers=hdr)
r3 = client.post("/auth/demo-login", json={"role": "admin"}, headers=hdr)
check("login is rate limited (2/min)", r.status_code == 200 and r3.status_code == 429, f"{r.status_code} {r3.status_code}")

print(f"\nRATE LIMIT GATE: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
