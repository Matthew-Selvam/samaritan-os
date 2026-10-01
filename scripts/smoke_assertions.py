"""
smoke_assertions.py — the Signal-OS smoke assertions (executable)
==============================================================
Extracted from ``scripts/smoke.sh`` so that the script can test either a backend
it starts itself or one somebody else is already running (``--base-url``, which
is what ``make test-smoke`` passes) without maintaining two copies of the same
checks. There is exactly one source of truth for these assertions; editing this
file changes both invocation paths.

The pytest twin of these assertions lives in ``backend/tests/test_smoke.py``.

Usage (normally invoked by smoke.sh, not by hand)::

    python smoke_assertions.py http://127.0.0.1:8766

Exits 0 when every check passes, 1 otherwise, printing the name of each failing
check to stderr.
"""

import json
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1]
FAIL = []
CHECKS = 0


def check(label, condition, detail=""):
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  ok {label}", flush=True)
    else:
        print(f"  ERR {label}: {detail}", flush=True)
        FAIL.append(label)


def call(method, path, payload=None, expect=200):
    url = BASE + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(  # noqa: S310 — BASE is a local http(s) URL
        url, data=data, headers=headers, method=method
    )
    try:
        # noqa is safe here: BASE is an operator-supplied http(s) loopback URL.
        with urllib.request.urlopen(req, timeout=180) as r:  # noqa: S310
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body or b"{}")
        except Exception:
            return e.code, {"raw": body.decode(errors="replace")[:400]}
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


def raw_get(path, timeout=60):
    """GET returning (status, content-type, body) without parsing.

    Needed for the non-JSON report formats (markdown, PDF), where forcing
    json.loads would fail on valid content.

    Args:
        path: Path and query, appended to the base URL.
        timeout: Seconds to wait.

    Returns:
        A ``(status, content_type, body_bytes)`` tuple; ``(0, "", b"")`` on a
        connection failure.
    """
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:  # noqa: S310
            return r.status, r.headers.get("content-type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("content-type", ""), e.read()
    except Exception:  # noqa: BLE001 — connection-level failure
        return 0, "", b""


print("=== ops surface", flush=True)
status, health = call("GET", "/api/health")
check("health 200", status == 200, f"got {status}")
# "degraded" is the *correct* answer with zero infrastructure running: the
# store fell back to memory. What must never happen is "down".
check(
    "health status is ok or degraded",
    health.get("status") in ("ok", "degraded"),
    str(health.get("status")),
)
check("health names the platform", health.get("platform") == "signal-os", str(health)[:200])

status, deep = call("GET", "/api/health/deep")
check("health/deep 200", status == 200, f"got {status}")
check("health/deep reports a status", bool(deep.get("status")), str(deep)[:200])

status, metrics = call("GET", "/api/metrics")
check("metrics 200", status == 200, f"got {status}")

status, cfg = call("GET", "/api/config")
check("config 200", status == 200, f"got {status}")

print("=== agents", flush=True)
status, agents = call("GET", "/api/agents")
names = agents.get("items") if isinstance(agents, dict) else None
names = [a.get("name") for a in names] if isinstance(names, list) else None
check("agents 200", status == 200, f"got {status}")
check(
    "16 agents registered",
    bool(names) and len(names) == 16,
    f"got {len(names) if names else 0}: {names}",
)
check("APEX is present", bool(names) and "APEX" in names, str(names)[:200])
check("QUILL is present", bool(names) and "QUILL" in names, str(names)[:200])

print("=== the real pipeline", flush=True)
t0 = time.perf_counter()
status, queued = call(
    "POST",
    "/api/investigate",
    {"input": "example.com", "input_type": "domain"},
)
elapsed = time.perf_counter() - t0
check("investigate 200", status == 200, f"got {status}: {str(queued)[:300]}")
inv_id = queued.get("inv_id")
case_id = queued.get("case_id")
check("investigate returns inv_id", bool(inv_id), str(queued)[:300])
check("investigate returns case_id", bool(case_id), str(queued)[:300])
print(f"  ok queued {inv_id} in {elapsed:.2f}s", flush=True)

# The async path is fire-and-forget; poll the record like a real client would.
rec = {}
deadline = time.time() + 120
while time.time() < deadline:
    status, rec = call("GET", f"/api/investigate/{inv_id}")
    if status == 200 and rec.get("status") in ("done", "completed", "error", "failed"):
        break
    time.sleep(0.5)

check(
    "investigation reached a terminal state",
    rec.get("status") in ("done", "completed", "error", "failed"),
    f"status={rec.get('status')!r} after {120 - int(deadline - time.time())}s",
)
check(
    "investigation did not error",
    rec.get("status") in ("done", "completed"),
    f"error={rec.get('error')!r}",
)

report = rec.get("report")
check("report is present", bool(report), "report was empty/None")
check(
    "report is non-empty",
    bool(report) and len(str(report)) > 80,
    f"report length={len(str(report)) if report else 0}",
)

ran = rec.get("agents") or []
check(
    "pipeline recorded agent results",
    len(ran) >= 9,
    f"only {len(ran)} agents recorded: {[a.get('name') for a in ran]}",
)
# APEX synthesises the report and does not list itself in `agents`; the DAG
# members it *did* activate are what matters.
ran_names = [a.get("agent") for a in ran]
for required in ("SCOUT", "NEXUS", "KRONOS", "VAULT", "SENTINEL", "QUILL"):
    check(f"{required} ran", required in ran_names, str(ran_names))

# The reasoning agents must not hard-fail offline. Fetching agents (CRAWLER)
# legitimately error with no network — that is the documented degradation
# contract, not a regression.
reasoning = {"SCOUT", "NEXUS", "KRONOS", "VAULT", "SENTINEL", "QUILL"}
degraded = [
    a.get("agent") for a in ran if a.get("status") == "error" and a.get("agent") in reasoning
]
check("no reasoning agent hard-failed", not degraded, f"degraded: {degraded}")

print("=== reports", flush=True)
# `call` parses JSON, which is wrong for markdown/PDF: assert on raw HTTP
# status and content type instead, and only parse for JSON.
for fmt, prefix in (("markdown", b"# Intelligence Report"), ("json", b"{"), ("pdf", b"%PDF")):
    raw_status, raw_ct, raw_body = raw_get(f"/api/investigate/{inv_id}/report?format={fmt}")
    check(f"report?format={fmt} 200", raw_status == 200, f"got {raw_status}")
    check(f"report?format={fmt} is non-empty", bool(raw_body), f"{len(raw_body)} bytes")
    check(f"report?format={fmt} content looks right", raw_body.startswith(prefix), raw_body[:40])

print("=== listing", flush=True)
status, listing = call("GET", "/api/investigations?limit=5")
check("investigations 200", status == 200, f"got {status}")
check("investigations is a list", isinstance(listing, list), type(listing).__name__)
check(
    "our investigation is listed",
    isinstance(listing, list) and any(r.get("inv_id") == inv_id for r in listing),
    "inv_id absent from the listing",
)

print("=== cases", flush=True)
status, created = call("POST", "/api/cases", {"name": f"smoke-{int(time.time())}"})
check("case create 200", status == 200, f"got {status}: {str(created)[:200]}")
new_case = created.get("case") if isinstance(created, dict) else None
cid = (new_case or {}).get("case_id") or created.get("case_id")
check("case create returns case_id", bool(cid), str(created)[:300])

if cid:
    status, got = call("GET", f"/api/cases/{cid}")
    check("case get 200", status == 200, f"got {status}")
    status, patched = call("PATCH", f"/api/cases/{cid}", {"notes": "smoke test"})
    check("case patch 200", status == 200, f"got {status}")
    status, stats = call("GET", f"/api/cases/{cid}/stats")
    check("case stats 200", status == 200, f"got {status}")
    status, _ = call("DELETE", f"/api/cases/{cid}")
    check("case delete 200", status == 200, f"got {status}")

print("=== negative cases (the guard rails)", flush=True)


# Each of these must be refused. Which status counts as "refused" depends on the
# probe: a malformed body is 400/422, an unknown-but-well-formed id is a clean
# 404, and 429 also counts because an instance running with the production
# 6 rpm investigate budget can exhaust its burst bucket partway through this
# list. What must never happen is a 200 or a 500.
def refused(label, method, path, payload=None, ok_statuses=(400, 422, 429)):
    status, body = call(method, path, payload)
    check(label, status in ok_statuses, f"got {status}: {str(body)[:160]}")


refused("empty input rejected", "POST", "/api/investigate", {"input": ""})
refused(
    "unknown field rejected (extra=forbid)",
    "POST",
    "/api/investigate",
    {"input": "x", "bogus_field": 1},
)
refused(
    "path traversal id refused",
    "GET",
    "/api/investigate/..%2F..%2Fetc%2Fpasswd",
    ok_statuses=(400, 404, 422, 429),
)
refused(
    "unknown agent refused", "GET", "/api/agents/NOT_AN_AGENT", ok_statuses=(400, 404, 422, 429)
)
# The traversal probe must not have leaked file contents on any path.
status, body = call("GET", "/api/investigate/..%2F..%2Fetc%2Fpasswd")
check("traversal probe leaked nothing", "root:" not in str(body), str(body)[:160])

print()
if FAIL:
    print(f"FAILED {len(FAIL)}/{CHECKS} checks:", file=sys.stderr)
    for name in FAIL:
        print(f"  - {name}", file=sys.stderr)
    sys.exit(1)
print(f"PASSED all {CHECKS} checks (inv_id={inv_id}, {elapsed:.2f}s to queue)", flush=True)
