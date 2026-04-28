# A19 — Phase E re-audit #14

## 1. Header

- **Model:** claude_opus_4_7
- **Commit:** `dd02a2d`
- **Scope:** re-audit of `/home/user/workspace/mariana` on branch `loop6/zero-bug` at commit `dd02a2d`, with required re-read of `loop6_audit/REGISTRY.md`, `loop6_audit/P01_FIX_REPORT.md`, and `loop6_audit/A18_phase_e_reaudit.md`. Adversarial probing of the P-01 CAS UPSERT fix in `mariana/agent/loop.py:_persist_task` (lines 80-210), the pre-flight DB re-read in `run_agent_task` (lines 822-864), the finally-block defense in depth (lines 1019-1078), the stop endpoint at `mariana/agent/api_routes.py:756-869`, the stripe webhook entry at `mariana/api.py:5600-5680`, the agent settlement helper at `mariana/agent/loop.py:344-487`, and the agent state machine at `mariana/agent/state.py:19-43`. Tests confirmed: 328 passing, 13 skipped.

## 2. Surface walkthrough / explicit P-01 fix probes

### 2.1 CAS UPSERT WHERE clause (`mariana/agent/loop.py:91-210`)

The `ON CONFLICT (id) DO UPDATE … WHERE NOT (…)` predicate is

```sql
WHERE NOT (
    agent_tasks.credits_settled = TRUE
    AND agent_tasks.state IN ('done','failed','halted','cancelled')
    AND EXCLUDED.credits_settled = FALSE
)
```

i.e. the UPDATE is **rejected only when all three conditions hold**:

1. existing row already settled; **AND**
2. existing row already in a terminal state; **AND**
3. incoming row tries to set `credits_settled = FALSE`.

Probes:

- **Worker-runs-to-DONE while CANCELLED+settled in DB.** `task.credits_settled` is set to `True` at `loop.py:1057` after the finally-block DB re-read, so `EXCLUDED.credits_settled = TRUE` — condition 3 is False — the CAS does **not** reject the UPDATE. The UPDATE clobbers `state='cancelled'` to `state='halted'` (or `'done'`/`'failed'`). **See finding Q-01 below.**
- Stop endpoint UPSERT path (`api_routes.py:857`): stop endpoint sets `terminal_task.credits_settled=True` before persisting, so the same condition-3 hole exists in reverse. Not exploitable here because the stop endpoint UPSERTs FIRST in this race; the worker is the late writer.
- Worker reaches terminal DONE/FAILED with `credits_settled=False` (M-01 happy path settle hasn't run yet): condition 1 is False — UPSERT lands. ✓ correct.
- asyncpg `cmd_tag` parsing verified empirically against the live local Postgres (`PGHOST=/tmp PGPORT=55432`):
  - CAS-rejected case → `'INSERT 0 0'` → `affected=0` → `_persist_task` returns `False`. ✓
  - CAS-passed case → `'INSERT 0 1'` → `affected=1` → returns `True`. ✓
- Autocommit / rollback: `_persist_task` opens an `async with db.acquire() as conn:` and runs `conn.execute(…)` outside any explicit transaction, so the statement autocommits per asyncpg semantics. There is no ambient transaction to roll back, and a subsequent crash mid-finally cannot un-do the UPSERT. ✓

### 2.2 Pre-flight DB re-read (`loop.py:822-864`)

- Stop runs before the new `fetchrow`: pre-flight reads `state in (terminal) AND credits_settled=TRUE` and early-returns without mutating `task.state`, so the finally `is_terminal(task.state)` guard stays False. ✓
- Stop runs while pre-flight `fetchrow` is in flight: stop holds `FOR UPDATE` on the row inside its transaction (`api_routes.py:786`). The pre-flight `fetchrow` is a plain `SELECT` (no `FOR UPDATE`, no `FOR SHARE`); under default `READ COMMITTED` it does **not** block on the writer's row-lock and may return the pre-stop snapshot. That is acceptable here because the CAS guard catches the subsequent UPSERT.
- Stop runs between pre-flight and the next code line (`await _persist_task(db, task)` at line 867): same story — the UPSERT either races stop's transaction (waits on the conflict-update row lock) or runs after stop commits, in which case the CAS guard fires. ✓
- Stop runs DURING `planner.build_initial_plan`: planner cost is incurred to `task.spent_usd` (line 894). The next `_persist_task` (line 896) is CAS-rejected. The execute-loop `_check_stop_requested` then halts the worker and the finally re-reads + skips settle. **However** the `_persist_task` at line 1074 ultimately writes `task.spent_usd` (planner cost) over the stop endpoint's `spent_usd=0` *and* clobbers `state` (see Q-01). That means planner cost is recorded but the user already received a full reservation refund — net financial leak ≈ planner LLM call cost (commonly $0.01–$0.10 per Opus call, but unbounded by `budget_usd`). This is the same exploit vector as Q-01.

### 2.3 Finally re-read (`loop.py:1019-1078`)

- DONE + DB shows settled=True: re-read sees True, sets in-memory `credits_settled=True`, skips `_settle_agent_credits`. **But then the trailing `_persist_task` (line 1074) submits state=DONE+credits_settled=True over a DB row that may be CANCELLED+settled.** CAS allows because `EXCLUDED.credits_settled = TRUE`. State changes from `cancelled` → `done`. (See Q-01.)
- DONE + DB shows settled=False: settle runs once, persist lands. ✓
- HALTED + DB shows settled=True (because stop endpoint pre-execution path settled): same hole as above. State changes `cancelled` → `halted`. (See Q-01.)

### 2.4 Stop endpoint (`api_routes.py:756-869`)

- `pre_execution = (state==PLAN AND spent_usd<=0 AND not already_settled)` (line 814-818). The check uses the under-`FOR UPDATE` view, so concurrent worker writes that occur after this snapshot can race the post-commit `_load_agent_task` at line 842. The settle uses `terminal_task.spent_usd` from that re-load — if the worker has not yet persisted planner cost, `spent_usd=0` and the full reservation is refunded; the worker's planner cost is later persisted via the Q-01 hole (see 2.2). Reservation refund precision is therefore wrong in the race window.
- Stop's settle runs OUTSIDE the FOR UPDATE transaction (transaction ends at line 823, settle at line 849, persist at line 857). That intentional split is what gives the worker a window to interleave a CAS-rejected `_persist_task`, but more importantly opens the Q-01 finally clobber.

### 2.5 Other surfaces re-checked (no new finding)

- **Stripe webhook signing** (`mariana/api.py:5600-5680`): signature verified via `_stripe.Webhook.construct_event` with primary/previous dual-secret rotation, before idempotency claim and any side effect. ✓
- **`process_charge_reversal` RPC** (mig 021): callable via PostgREST with the service-role key from `_supabase_api_key(cfg)`; SECURITY DEFINER + per-charge `pg_advisory_xact_lock`. anon/authenticated EXECUTE has been revoked by mig 005/011 family; not anon-callable. ✓
- **Free-tier signup grant abuse**: `handle_new_user` (mig 011) inserts a profile + bucket via SECURITY DEFINER with `ON CONFLICT (id) DO NOTHING`; gated by `auth.users` insert which Supabase requires email verification on per project settings. No code-level loophole observed in this pass.
- **Prompt injection via `goal` / `user_instructions`**: planner.py `_format_goal` JSON-quotes input into the LLM message; tool dispatch in `dispatcher.py` does not directly exec the goal string. No new injection surface beyond known LLM-prompt-injection class.
- **Artifact path traversal**: `mariana/agent/tools.py` artifact write paths use `os.path.join(workspace_root, …)` followed by a startswith check; not new.
- **Stream-token JWT secret exposure**: `frontend/src/lib/streamAuth.ts` mints a stream-token via `/api/stream-token`; the server-side mint uses the Supabase JWT secret, not exposed to the browser. ✓
- **`credit_transactions` trigger / RLS**: anon/authenticated INSERT EXECUTE revoked; only `add_credits`/`grant_credits`/`refund_credits` SECURITY DEFINER paths can INSERT, and they fix `type` server-side. No phony `type='grant'` route. ✓
- **Conversation/message IDOR**: `/api/conversations/{id}` checks `current_user.user_id == row['user_id']` (api.py); not new.

## 3. Findings

### Q-01 — P2 — finally-block UPSERT clobbers terminal state from CANCELLED (or any peer terminal) when worker reaches its own terminal state in the same race window

- **Severity:** P2
- **Surface:** agent lifecycle / cancel state contract / CAS-guard hole
- **Root cause file:line:**
  - `mariana/agent/loop.py:157-161` — CAS predicate only blocks UPDATE when `EXCLUDED.credits_settled = FALSE`, leaving a hole when EXCLUDED is `TRUE`.
  - `mariana/agent/loop.py:1052-1057` — finally-block sets `task.credits_settled = True` after the DB re-read, *then* calls `_persist_task` at line 1074, which now passes the CAS guard and overwrites `state`.
- **Exploit / impact:**
  1. User starts an agent run with `budget_usd=5.0` (reserves 500 credits). Worker BLPOPs and loads the stale snapshot.
  2. Worker passes the new pre-flight DB re-read (DB still shows `state=PLAN, credits_settled=False`).
  3. Worker calls `planner.build_initial_plan` — incurs cost `$X` (Opus-class planner call, not bounded by 0).
  4. Concurrently, user hits Stop. Stop endpoint locks the row, sees `pre_execution=True` (because the worker has not yet persisted planner cost — the worker's UPSERT at line 896 is currently waiting on the FOR UPDATE row-lock or has not yet started). Stop endpoint sets `stop_requested=TRUE` + commits, then re-loads (`spent_usd=0` still) and runs `_settle_agent_credits` which **refunds the full 500 credits** via `add_credits`. Stop endpoint persists `state=CANCELLED, credits_settled=TRUE, spent_usd=0`.
  5. Worker's planner-cost `_persist_task` at line 896 fires next; CAS rejects (correct). Worker's `_check_stop_requested` at line 908 returns True (Redis stop key is set). Worker calls `_transition(…, HALTED)` which `_persist_task`s state=HALTED+credits_settled=False — CAS rejects (correct).
  6. Worker hits the finally block. `is_terminal(task.state)=True` (state=HALTED in memory). DB re-read returns `credits_settled=True`. Worker sets `task.credits_settled = True` and skips settle (correct — no double refund).
  7. Worker calls `_persist_task` at line 1074. EXCLUDED is `state=halted, credits_settled=TRUE, spent_usd=$X`. CAS predicate condition 3 (`EXCLUDED.credits_settled = FALSE`) is False, so the CAS does **not** reject. `ON CONFLICT DO UPDATE` runs. **Final DB row: `state=halted, spent_usd=$X, credits_settled=TRUE`.**

  Empirical confirmation against the live local Postgres:

  ```
  Rejected case cmd_tag = 'INSERT 0 0'
  Passed (state-clobber) cmd_tag = 'INSERT 0 1'
  Final row = {'state': 'halted', 'credits_settled': True}
  ```

  Two impacts:

  - **Cancel-state contract violation.** The user (and any consumer that branches on `state`) sees `halted` instead of `cancelled`. Frontend (`frontend/src/components/deft/projects/ProjectRow.tsx:108`, `Build.tsx:49`, `LiveCanvas.tsx:88`, `PreviewPane.tsx:44`) treats both as terminal but renders different copy/styling and reports different audit semantics. The same hole also lets a worker that reaches DONE in the deliver path (no stop-checks during `_deliver`) clobber a CANCELLED row to DONE — which actively misrepresents a cancelled task as a successful delivery.
  - **Free planner cost leak.** Stop-endpoint settled at the pre-race `spent_usd=0` (full 500-credit refund), but the worker's planner-LLM cost `$X` is then persisted into `agent_tasks.spent_usd`. Because `credits_settled` is already `TRUE`, no further deduct ever fires. The user pays nothing for the real LLM call. The leak is small per-incident (≈$0.01–$0.10) but trivially repeatable from a race-stop loop and unbounded by `budget_usd`.

  This is **not** a duplicate of P-01: P-01 was about the worker un-flipping `credits_settled` to False (double refund). The fix correctly closed that path. Q-01 is the symmetric hole — the worker keeps `credits_settled=TRUE` (per the finally re-read) but still rewrites `state` and `spent_usd` over the stop-endpoint's terminal row.

- **Fix sketch:**
  1. Tighten the CAS guard so any incoming write that targets an *already terminal + settled* row is rejected unless the incoming `state` matches the existing terminal `state` *and* nothing materially changes:

     ```sql
     WHERE NOT (
         agent_tasks.credits_settled = TRUE
         AND agent_tasks.state IN ('done','failed','halted','cancelled')
         AND (
             EXCLUDED.credits_settled = FALSE
             OR EXCLUDED.state <> agent_tasks.state
         )
     )
     ```

     This blocks both the un-finalize hole and the state-clobber hole while still letting the *original* finalizer's idempotent re-write succeed.
  2. Defense in depth: in the worker's finally branch, if the DB re-read at line 1037 shows `credits_settled=TRUE`, also re-read `state` and skip the trailing `_persist_task` entirely (or copy DB state into `task.state` before persisting). Currently only `credits_settled` is round-tripped.
  3. Add a regression test that sets up a `state=cancelled, credits_settled=TRUE` DB row, then drives `run_agent_task` with a stale snapshot that progresses to HALTED (and separately to DONE) and asserts `state` remains `'cancelled'` and `spent_usd` is unchanged.

## 4. Additional rationale / no-second-finding notes

I deliberately re-challenged each surface listed in the brief and did not promote separate findings for:

- **CAS-guard rejection / cmd_tag parsing**: confirmed empirically against local Postgres — `INSERT 0 0` vs `INSERT 0 1` parse correctly to `affected=0`/`1`.
- **Pre-flight DB re-read TOCTOU vs FOR UPDATE**: the new `fetchrow` is a plain SELECT under `READ COMMITTED` and does not block on the stop endpoint's row lock; that is acceptable because the CAS guard catches subsequent stale UPSERTs (modulo Q-01).
- **Stripe webhook signing / replay**: `_stripe.Webhook.construct_event` runs before `_claim_webhook_event`; dual-secret rotation handles in-flight events; no fresh defect.
- **`process_charge_reversal` RPC**: SECURITY DEFINER with anon EXECUTE revoked; api.py POST uses service-role key.
- **Free-tier signup grant**: handler is gated on `auth.users` insert; no code-level loophole found.
- **Prompt injection / artifact traversal**: existing JSON-quoting and path-prefix checks are intact.
- **Stream-token JWT secret**: not exposed to the browser; mint endpoint uses server-only secret.
- **credit_transactions RLS**: anon/authenticated INSERT remains revoked; ledger writes go through SECURITY DEFINER RPCs only.
- **Conversation/message IDOR**: existing owner check still in place.

RE-AUDIT #14 COMPLETE findings=1 file=loop6_audit/A19_phase_e_reaudit.md
