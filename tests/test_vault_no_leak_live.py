"""F4 vault no-leak live test — runs against a deployed Mariana.

Boots a real agent task with a vault_env containing a unique tracer
secret and waits until the task reaches a terminal state.  Then it
queries every event row, every step result, and the final task JSON
for the tracer string.  Pass condition: ZERO occurrences of plaintext.

Skipped unless DEFT_LIVE=1 is set so the unit suite stays hermetic.

Required env:
  DEFT_LIVE=1
  DEFT_BASE                — e.g. http://77.42.3.206:8080
  SUPABASE_URL
  SUPABASE_ANON_KEY
  SUPABASE_SERVICE_KEY     — to query agent_events directly via REST
  DEFT_TEST_EMAIL          — test user email
  DEFT_TEST_PASSWORD       — test user password
"""

from __future__ import annotations

import json
import os
import secrets
import string
import time
from typing import Any

import httpx
import pytest

LIVE = os.getenv("DEFT_LIVE") == "1"
BASE = os.getenv("DEFT_BASE", "http://77.42.3.206:8080").rstrip("/")
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_ANON = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE = os.getenv("SUPABASE_SERVICE_KEY", "")
EMAIL = os.getenv("DEFT_TEST_EMAIL", "testrunner@mariana.test")
PASSWORD = os.getenv("DEFT_TEST_PASSWORD", "DeftTest!2026")

pytestmark = pytest.mark.skipif(not LIVE, reason="set DEFT_LIVE=1 to run")


def _login() -> str:
    r = httpx.post(
        f"{SUPABASE_URL}/auth/v1/token",
        params={"grant_type": "password"},
        headers={"apikey": SUPABASE_ANON, "Content-Type": "application/json"},
        json={"email": EMAIL, "password": PASSWORD},
        timeout=15.0,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _start_task(token: str, prompt: str, vault_env: dict[str, str]) -> str:
    r = httpx.post(
        f"{BASE}/api/agent",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "goal": prompt,
            "selected_model": "claude-sonnet-4-6",
            "budget_usd": 0.5,
            "max_duration_hours": 0.25,
            "vault_env": vault_env,
        },
        timeout=20.0,
    )
    if r.status_code != 202:
        raise RuntimeError(f"start failed: {r.status_code} {r.text}")
    return r.json()["task_id"]


def _wait_terminal(token: str, task_id: str, timeout: float = 240.0) -> dict[str, Any]:
    terminal = {"done", "completed", "failed", "stopped", "cancelled", "error", "halted"}
    deadline = time.time() + timeout
    last: dict[str, Any] = {}
    while time.time() < deadline:
        r = httpx.get(
            f"{BASE}/api/agent/{task_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15.0,
        )
        if r.status_code == 200:
            last = r.json()
            if last.get("state") in terminal:
                return last
        time.sleep(3)
    raise TimeoutError(f"task did not terminate in {timeout}s; last state={last.get('state')}")


def _fetch_events(token: str, task_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    after = 0
    while True:
        r = httpx.get(
            f"{BASE}/api/agent/{task_id}/events",
            params={"after_id": after, "limit": 1000},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15.0,
        )
        r.raise_for_status()
        body = r.json()
        evs = body.get("events", [])
        if not evs:
            break
        out.extend(evs)
        after = evs[-1]["id"]
        if len(evs) < 1000:
            break
    return out


def _random_tracer(label: str = "DEFTLEAKCHECK") -> str:
    """A long, distinctive tracer string that will be redacted if vault works."""
    alphabet = string.ascii_uppercase + string.digits
    return f"{label}_" + "".join(secrets.choice(alphabet) for _ in range(40))


def test_vault_env_does_not_leak_plaintext_through_events_or_results():
    token = _login()
    tracer = _random_tracer()
    name = "DEFT_LEAK_TRACER"
    # Prompt the LLM to print the env var so we exercise the redaction path
    # of stdout AND step.result AND event payloads.
    prompt = (
        f"Run a single Python step that prints the value of the environment "
        f"variable {name} and nothing else.  Then deliver."
    )
    task_id = _start_task(token, prompt, {name: tracer})
    final = _wait_terminal(token, task_id)
    events = _fetch_events(token, task_id)

    # Serialise every event payload + every step result + final answer to one
    # blob and grep for the tracer.
    blob = json.dumps({"final": final, "events": events})
    assert tracer not in blob, (
        f"PLAINTEXT LEAK: tracer {tracer!r} found in events/results "
        f"(state={final.get('state')}, n_events={len(events)})"
    )
    # Sanity: we should at least see one [REDACTED:DEFT_LEAK_TRACER] marker
    # somewhere if the LLM successfully echoed the var.  This is advisory —
    # planner/dispatcher behaviour can vary across runs, so we don't fail
    # on its absence (the no-leak invariant is the hard guarantee).
    if f"[REDACTED:{name}]" in blob:
        print(f"observed redaction marker for {name}")


def test_invalid_vault_env_payload_returns_422():
    token = _login()
    r = httpx.post(
        f"{BASE}/api/agent",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "goal": "no-op",
            "vault_env": {"lower_case_invalid": "secretvalue1234"},
        },
        timeout=15.0,
    )
    assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text}"
    assert "vault_env" in r.text.lower()
