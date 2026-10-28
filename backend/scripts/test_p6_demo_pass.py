"""P6 demo pass (live, no mocking) — a representative subset of
DEMO_SCENARIOS.md's canonical beats across all 3 scenarios, through the real
agent path. Not exhaustive (phrasing variants, Kannada, and exact DBSCAN
cluster-count determinism are out of scope for this pass) — reports actual
observed behavior against the acceptance criteria's core principles.

Run: cd backend && ASK_ENGINE=agent python -m scripts.test_p6_demo_pass
"""

import json
import os
import sys
from pathlib import Path

os.environ["ASK_RATE_LIMIT_PER_MINUTE"] = "0"  # this script fires many /ask calls from one client
os.environ["ASK_RATE_LIMIT_PER_HOUR"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starlette.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
import app.routers.ask as askmod  # noqa: E402
from app.roles import Role  # noqa: E402
from app.schemas import CurrentUser  # noqa: E402

_USERS = {
    "io": CurrentUser(id="io-1", role=Role.INVESTIGATING_OFFICER, officer_id="KSP-23417"),
    "supervisor": CurrentUser(id="sup-1", role=Role.SUPERVISOR, officer_id="KSP-10002"),
    "analyst": CurrentUser(id="an-1", role=Role.ANALYST, officer_id="KSP-10003"),
    "admin": CurrentUser(id="adm-1", role=Role.ADMIN, officer_id=None),
}

client = TestClient(m.app)
HDR = {"Authorization": "Bearer faketoken"}

results: list[dict] = []


def ask(role_key: str, query: str, thread_id: str | None = None) -> dict:
    askmod.verify_jwt = lambda token, settings: _USERS[role_key]
    body = {
        "query": query, "turn_index": 0, "input_modality": "text", "client_ts": "t",
        **({"thread_id": thread_id} if thread_id else {}),
    }
    r = client.post("/ask", json=body, headers=HDR)
    data = r.json() if r.status_code == 200 else {"error": r.text}
    return {"status": r.status_code, "data": data}


def record(beat: str, role: str, query: str, expect: str, actual_types: list[str], reason: str | None, ok: bool):
    results.append({
        "beat": beat, "role": role, "query": query, "expect": expect,
        "actual": actual_types, "reason": reason, "ok": ok,
    })
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {beat:8s} ({role:10s}) expect={expect:20s} actual={actual_types} reason={reason}")


def run_beat(beat: str, role: str, query: str, expect_types: set[str], thread_id: str | None = None) -> dict:
    resp = ask(role, query, thread_id)
    if resp["status"] != 200:
        record(beat, role, query, str(expect_types), ["<http_error>"], None, False)
        return resp["data"]
    blocks = resp["data"].get("message", {}).get("blocks", [])
    types = [b["type"] for b in blocks]
    ok = bool(expect_types & set(types))
    record(beat, role, query, str(expect_types), types, blocks[0].get("reason") if types == ["no_answer"] else None, ok)
    return resp["data"]


def run_negative(beat: str, role: str, query: str, expect_reason: str, thread_id: str | None = None) -> None:
    resp = ask(role, query, thread_id)
    if resp["status"] != 200:
        record(beat, role, query, f"no_answer({expect_reason})", ["<http_error>"], None, False)
        return
    blocks = resp["data"].get("message", {}).get("blocks", [])
    types = [b["type"] for b in blocks]
    reason = blocks[0].get("reason") if blocks and blocks[0]["type"] == "no_answer" else None
    ok = types == ["no_answer"] and reason == expect_reason
    record(beat, role, query, f"no_answer({expect_reason})", types, reason, ok)


print("=== Scenario A — Chain Gang ===")
a1 = run_beat("A1", "io", "Check antecedents of suspect Ravi Kumara", {"text", "case_card", "table"})
a_thread = a1.get("thread_id")
run_beat("A2", "io", "Show me similar cases with this method as FIR KA-MYS-001-2026-0001", {"mo_match"}, a_thread)
run_beat("A2b", "io", "Where are chain snatching incidents concentrated near my station?", {"map", "table"})
run_beat("A3", "analyst", "Show the network around Ravi Kumara", {"network_graph", "text"})
run_beat("A4", "supervisor", "Give me a review pack for the Mysuru Chain Gang cases", {"pack_report", "text", "table"})
run_beat("A5", "admin", "Show query activity for this session", {"table", "text"})
run_negative("A-N1", "io", "Check antecedents of Prakash Jadhav", "not_found")
run_negative("A-N2", "admin", "Show me Ravi Kumara's FIRs", "out_of_scope")

print("\n=== Scenario B — Repeat Victim ===")
run_beat("B1", "io", "Show complaint history for the household at Jayanagar 4th Block, complainant Savitha R", {"text", "table"})
b2 = run_beat("B2", "io", "Is vehicle KA-05-MJ-4977 stolen?", {"case_card"})
run_beat("B4", "supervisor", "Show vehicle theft hotspots in Jayanagar station limits", {"map", "table"})
run_negative("B-N1", "io", "Is vehicle KA-53-ZX-0007 stolen?", "not_found")
run_negative("B-N2", "io", "check vehicle KA-5-MJJ-49", "invalid_reference")
run_negative("B-N3", "io", "Show FIR KA-BLR-099-2025-9999", "not_found")

print("\n=== Scenario C — Dasara Bandobast ===")
run_beat("C1", "supervisor", "What incidents happened during Dasara in Mysuru in previous years?", {"text", "table"})
run_beat("C2", "analyst", "Map the incident hotspots around the Dasara procession route in Mysuru", {"map", "table"})
run_beat("C4", "supervisor", "Prepare a bandobast brief for the upcoming Dasara", {"pack_report", "text", "table"})
run_negative("C-N2", "analyst", "Map incidents in Chandrapura Extension Phase 9", "not_found")

print(f"\n{'='*60}")
passed = sum(1 for r in results if r["ok"])
print(f"P6 DEMO PASS: {passed}/{len(results)} beats matched expectations")
print(f"{'='*60}")
for r in results:
    if not r["ok"]:
        print(f"  DID NOT MATCH: {r['beat']} — expected {r['expect']}, got {r['actual']} (reason={r['reason']})")
