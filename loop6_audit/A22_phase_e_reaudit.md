# Phase E re-audit #17

Header: model=gpt_5_4, commit=de3db80, scope=`14cbabd..de3db80` changed files plus broader sweep across S-01..S-04 follow-up risk, auth middleware, stream tokens, conversations, vault, frontend rendering, Stripe non-payment events, dynamic SQL, and ledger idempotency.

## Surface walkthrough

### 1. Required reading and diff scope

Read:
- `loop6_audit/REGISTRY.md`
- `loop6_audit/S01_S02_S03_S04_FIX_REPORT.md`
- `loop6_audit/A21_phase_e_reaudit.md`

Walked every changed file in `14cbabd..de3db80`:
- `mariana/agent/loop.py`
- `mariana/agent/schema.sql`
- `mariana/agent/settlement_reconciler.py`
- `mariana/main.py`
- `tests/test_r01_settlement_idempotency.py`
- `tests/test_s01_rpc_signature_match.py`
- `tests/test_s02_check_constraints.py`
- `tests/test_s03_reconciler.py`
- `tests/test_s04_no_cascade.py`

Also swept:
- `mariana/api.py`
- `mariana/agent/api_routes.py`
- `mariana/vault/runtime.py`
- `frontend/src/lib/streamAuth.ts`
- `frontend/src/pages/Chat.tsx`
- `frontend/src/components/FileViewer.tsx`
- `mariana/billing/ledger.py`
- relevant Supabase migrations for `add_credits` / `deduct_credits`

### 2. S-01 RPC payload fix probes

#### 2.1 `ref_id` no longer reaches PostgREST
Confirmed `mariana/agent/loop.py:435-441, 607-615, 641-650`: `ref_id` is still computed at `loop.py:480` and stored only in `agent_settlements` via `_claim_settlement(...)` (`loop.py:520-528`), but it is absent from both RPC JSON payloads.

#### 2.2 SELECT→INSERT lost-race behavior
`_settle_agent_credits` now looks up the claim first (`loop.py:486-505`), inserts only if absent (`loop.py:515-528`), and on `ON CONFLICT DO NOTHING` re-fetches the row (`loop.py:538-555`). If the re-fetch still shows `completed_at IS NULL`, the loser logs `agent_credits_settle_claim_lost` and exits (`loop.py:562-569`). That matches the fix brief: no duplicate RPC, reconciler handles stuck rows later. Short-lived unsettled state remains possible for up to the reconciler threshold/cadence, but that is the intended contract.

#### 2.3 Q-01 CAS guard against stale `credits_settled=False` overwrite
Confirmed in `_persist_task` (`loop.py:155-181`): once the DB row is already settled, any incoming write with `EXCLUDED.credits_settled = FALSE` is rejected unless it is a true same-state settled self-write. So a stale worker cannot overwrite `credits_settled=TRUE` with `FALSE`.

### 3. S-02 CHECK constraint probes

Confirmed `agent_settlements` now enforces `reserved_credits >= 0` and `final_credits >= 0` in `mariana/agent/schema.sql:87-115`.

Searched settlement callers:
- `_settle_agent_credits` computes `final_tokens = int(task.spent_usd * 100)` and `delta = final_tokens - task.reserved_credits` (`loop.py:478-479`). Refund path uses `refund = abs(delta)` (`loop.py:643`).
- Reconciler reuses `_settle_agent_credits` and does not synthesize alternate values (`settlement_reconciler.py:126-147`).

So the new CHECKs do not reject legitimate refund/overrun flows; only `delta_credits` remains signed, which is correct.

### 4. S-03 reconciler probes

#### 4.1 Reconciler reload path
`reconcile_pending_settlements` reloads the canonical `AgentTask` via `_load_agent_task_from_row` → `_load_agent_task` (`settlement_reconciler.py:35-45, 126`; `agent/api_routes.py:136-198`) rather than using stored settlement-row values directly.

#### 4.2 Stale in-memory `credits_settled=True`
The reconciler explicitly resets `task.credits_settled = False` before retry (`settlement_reconciler.py:140-145`), so `agent_tasks.credits_settled=TRUE` no longer causes the helper to short-circuit.

#### 4.3 Locking / transaction lifetime
The reconciler does **not** hold a row lock across the RPC. Instead it atomically claims candidates by updating `claimed_at = now()` inside one `UPDATE ... WHERE task_id IN (SELECT ... FOR UPDATE SKIP LOCKED)` statement (`settlement_reconciler.py:81-107`). That means there is no autocommit `FOR UPDATE` bug here; the selection+claim is one SQL statement. Concurrent reconciler runs see disjoint work because once one process bumps `claimed_at`, the other process's age predicate no longer matches.

#### 4.4 Process overlap / singleton
There is no explicit in-process singleton guard in `main.py`; the loop simply runs every 60s (`main.py:852-885`). However, the `claimed_at` bump is the real concurrency control, and `tests/test_s03_reconciler.py:299-358` exercises two concurrent reconciler invocations successfully.

#### 4.5 Polling lag
The reconciler waits for `claimed_at < now() - max_age_seconds` with default `300s`, and the outer loop polls every `60s` (`settlement_reconciler.py:97-106`; `main.py:841-885`). So retries occur after roughly 5 minutes plus polling jitter, as documented.

### 5. S-04 RESTRICT probe

`agent_settlements.task_id` is now `ON DELETE RESTRICT` in `schema.sql:87-115`. Search found no production/admin deletion path for `agent_tasks`; only tests issue `DELETE FROM agent_tasks` (`tests/test_s04_no_cascade.py`, `tests/test_s03_reconciler.py`). So S-04 does not break a live cleanup route in this branch.

### 6. Architecture probe: ledger idempotency after `ref_id` removal

This is still the critical residual surface.

- `add_credits(p_user_id uuid, p_credits integer)` has no `(ref_type, ref_id)` input and no duplicate-collapse behavior (`frontend/supabase/migrations/018_i01_add_credits_lock.sql:9-76`).
- `deduct_credits(target_user_id uuid, amount integer)` likewise has no idempotency key (`frontend/supabase/migrations/007_loop6_b02_b05_b06_ledger_sync.sql:85-122`).
- Only the higher-level ledger RPCs `grant_credits` / `refund_credits` are idempotent on `(ref_type, ref_id)` (`mariana/billing/ledger.py:88-174`).

That means S-01's removal of `ref_id` from the low-level agent settlement RPCs is safe only if `agent_settlements.completed_at` remains a perfect once-only fence.

### 7. Broader sweep beyond S-01..S-04

#### 7.1 Auth middleware
`_authenticate_supabase_token` in `mariana/api.py:1216-1255` delegates JWT verification to `GET /auth/v1/user` on the configured Supabase project instead of trusting local JWT claims. I did not confirm a cross-project token acceptance bug in this branch.

#### 7.2 Stream auth tokens
Research SSE and agent SSE both use short-lived HMAC-signed task-scoped tokens (`api.py:1331-1457`, `agent/api_routes.py:595-643`, `frontend/src/lib/streamAuth.ts:1-102`). Lifetime is 120s, task binding is enforced, and the frontend no longer falls back to putting the raw JWT in the URL.

#### 7.3 Conversation creation race
`POST /api/conversations` still uses server-side creation with no client-supplied UUID (`api.py:2409-2427`). I did not find a same-UUID race surface here.

#### 7.4 Vault storage / redaction
`mariana/vault/runtime.py:98-150, 178-222` stores task vault env in Redis, deletes on cleanup, and recursively redacts string values before logging/persisting tool payloads. No new direct secret leak found in this pass.

#### 7.5 Frontend XSS
`Chat.tsx` escapes HTML before markdown transforms and constrains links to `https?://` (`frontend/src/pages/Chat.tsx:298-345`). `FileViewer.tsx` uses a sandboxed iframe for HTML (`frontend/src/components/FileViewer.tsx:366-375`). No new obvious XSS regression found.

#### 7.6 Stripe non-payment events
Current webhook switch handles `customer.subscription.deleted` but not `customer.deleted` or `invoice.payment_failed` (`api.py:5688-5695, 6291-6322`; grep found no handlers for the latter two). That is operationally incomplete, but I did not confirm a fresh credit-accounting exploit from the current behavior alone.

#### 7.7 Dynamic SQL
The dynamic SQL I found is allowlisted or constant-shaped, e.g. `update_research_task` validates columns against `_ALLOWED_TASK_COLUMNS` before composing SQL (`mariana/data/db.py:802-816`), and `list_agent_tasks` only concatenates a fixed `where` fragment built from parameter placeholders (`agent/api_routes.py:260-278`). No direct SQL injection found.

## Findings

### T-01 — Successful agent settlement RPC can be replayed by the reconciler if `completed_at` stamping fails after the ledger mutation

- **Severity:** P1
- **Surface:** agent billing / settlement idempotency / reconciler retry
- **Root cause:** `mariana/agent/loop.py:687-699`, `mariana/agent/settlement_reconciler.py:140-147`, plus non-idempotent low-level ledger RPCs in `frontend/supabase/migrations/018_i01_add_credits_lock.sql:9-76` and `frontend/supabase/migrations/007_loop6_b02_b05_b06_ledger_sync.sql:85-122`

#### Root cause
The S-01/S-03 design makes `agent_settlements.completed_at` the sole once-only fence for agent settlement RPCs. But `_settle_agent_credits` treats a post-RPC failure to stamp `completed_at` as non-fatal:

```python
if rpc_succeeded:
    task.credits_settled = True
    if db is not None:
        try:
            await _mark_settlement_completed(db, task.id)
        except Exception as exc:
            logger.warning("agent_settlement_mark_completed_failed", ...)
```

If the ledger RPC already succeeded but `_mark_settlement_completed(...)` fails (transient DB error, pool hiccup, connection reset, statement timeout), the code leaves the claim row with `completed_at IS NULL` while the in-memory task is now `credits_settled=True`.

That by itself would merely create an inconsistent row. The real bug is that the reconciler later **forces** `task.credits_settled = False` (`settlement_reconciler.py:140-145`) and retries any claim whose `completed_at IS NULL` by calling `_settle_agent_credits(...)` again (`settlement_reconciler.py:146-147`).

Because the underlying low-level RPCs `add_credits` and `deduct_credits` do **not** accept or enforce any idempotency key, the second RPC is a second real ledger mutation, not a harmless replay.

#### Why this is new and not already covered by R-01/S-01/S-03
R-01 protected the pre-RPC duplicate-settle race using a claim row. S-01 fixed the bad PostgREST payload. S-03 added a reconciler for genuinely failed RPCs. None of the new tests cover the path where:
1. RPC returns 200,
2. `completed_at` update fails,
3. `credits_settled=True` is persisted,
4. reconciler later retries the same still-uncompleted claim.

`tests/test_s01_rpc_signature_match.py` only covers RPC success and RPC failure, not post-success marker failure. `tests/test_s03_reconciler.py` only covers rows whose original RPC failed and remained uncompleted.

#### Reproduction
I reproduced this locally with a harness that patches `_mark_settlement_completed` to fail once after a successful refund RPC, then runs the reconciler.

Evidence saved at `loop6_audit/A22_double_settle_repro.txt`.

Observed output:
- first `_settle_agent_credits` call performs one successful `add_credits` RPC, logs `agent_settlement_mark_completed_failed`, and leaves `completed_at=NULL` while `agent_tasks.credits_settled=TRUE`
- after aging the claim and running `reconcile_pending_settlements`, the reconciler issues a **second** identical `add_credits` RPC for the same task
- only then does `completed_at` become non-NULL

This is exactly the dangerous state transition:

```text
after settle calls 1 None True
after reconcile calls 2
[{'url': '.../rpc/add_credits', 'json': {'p_user_id': '...', 'p_credits': 470}},
 {'url': '.../rpc/add_credits', 'json': {'p_user_id': '...', 'p_credits': 470}}]
```

#### Impact
This re-introduces double-settlement risk through the new reconciler path:

- **Refund case (`delta < 0`)**: user receives the refund twice; credits are minted.
- **Overrun case (`delta > 0`)**: user is charged twice; credits are over-deducted.

The blast radius is larger than a cosmetic inconsistency because `add_credits` and `deduct_credits` mutate `profiles.tokens` directly and do not deduplicate by `ref_id`.

The trigger condition is realistic: any transient DB failure between the successful HTTP 200 and the `UPDATE agent_settlements SET completed_at = now()` write is enough. Since `_mark_settlement_completed` failure is explicitly swallowed, the worker continues and the row quietly becomes reconciler-eligible 5 minutes later.

#### Fix sketch
Any of the following would close the hole:

1. **Do not persist `task.credits_settled=True` unless `completed_at` was successfully stamped.**
   - Move `task.credits_settled = True` after `_mark_settlement_completed(...)` succeeds.
   - If marking fails, leave both the in-memory flag and DB row unset so the task remains visibly unsettled.

2. **Store an explicit “rpc_succeeded” / “ledger_applied” marker in `agent_settlements` separate from `completed_at`, and make the reconciler skip rows where the ledger already succeeded.**
   - Current design conflates “ledger mutation not done yet” with “marker write failed”.

3. **Preferably restore ledger-side idempotency for agent settlements.**
   - Add a dedicated idempotent RPC shape such as `agent_settle_refund(..., p_ref_id)` / `agent_settle_deduct(..., p_ref_id)` or route agent settlements through `grant_credits` / `refund_credits`-style primitives that already dedupe on `(ref_type, ref_id)`.
   - Without ledger-side dedup, any future marker-loss bug becomes a financial replay bug.

4. **Add a regression test** for “RPC 200 + `_mark_settlement_completed` failure + reconciler retry = still only one ledger mutation”.

## No other new findings confirmed in this pass

- S-01 payload fix is correctly implemented: no `ref_id` reaches PostgREST.
- SELECT/INSERT race handling now re-fetches and exits without duplicate RPC.
- Q-01 CAS still blocks stale `credits_settled=False` overwrite of settled rows.
- S-02 constraints are compatible with both refund and overrun paths.
- Reconciler concurrency uses `claimed_at` update plus `FOR UPDATE SKIP LOCKED` in one statement; I did not find a lock-lifetime bug there.
- S-04 `ON DELETE RESTRICT` does not break a production agent-task deletion path in this branch.
- Auth middleware, stream tokens, vault redaction, frontend markdown rendering, and the allowlisted dynamic SQL sites did not yield an additional confirmed bug in this pass.

RE-AUDIT #17 COMPLETE findings=1 file=loop6_audit/A22_phase_e_reaudit.md
