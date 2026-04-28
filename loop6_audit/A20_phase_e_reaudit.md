# Phase E re-audit #15

Header: model=gpt_5_4, commit=5891e70, scope=`e4b7cb7..5891e70` changed files plus broader sweep across agent billing, auth/session, conversation ownership, preview/storage, webhook/RPC privileges, queue recovery, and vault secret handling.

## Surface walkthrough

### 1. Required reading and diff scope
- Read prior context in `loop6_audit/REGISTRY.md`, `loop6_audit/Q01_FIX_REPORT.md`, and `loop6_audit/A19_phase_e_reaudit.md`.
- Walked every changed file in `e4b7cb7..5891e70`: `mariana/agent/loop.py`, `tests/test_q01_cas_state_clobber.py`, and the audit notes.
- Enumerated all `_persist_task(...)` call sites in `mariana/agent/loop.py` and `mariana/agent/api_routes.py` to verify whether any caller incorrectly assumes the UPSERT always lands.

### 2. Explicit Q-01 fix probes

#### `_persist_task` CAS clause
- `_persist_task` now blocks writes to an already-settled row unless the incoming snapshot preserves both `state` and `credits_settled=TRUE` at `mariana/agent/loop.py:155-181`.
- The exact hardened predicate is:
  ```sql
  WHERE (
      agent_tasks.credits_settled = FALSE
      OR (
          agent_tasks.state = EXCLUDED.state
          AND EXCLUDED.credits_settled = TRUE
      )
  )
  ```
- This does close the direct Q-01 state-clobber hole: stale `HALTED/settled=True` can no longer overwrite settled `CANCELLED` rows, and stale post-settlement `spent_usd` writes are blocked when the state differs.
- The same-state idempotent branch remains intentionally allowed, consistent with `tests/test_q01_cas_state_clobber.py`.

#### `finally:` short-circuit in `run_agent_task`
- The `finally:` block re-reads `credits_settled, state` from `agent_tasks` at `mariana/agent/loop.py:1054-1074`.
- If `already_settled_in_db` is true, it correctly skips both `_settle_agent_credits(task)` and the trailing `_persist_task(...)` at `mariana/agent/loop.py:1075-1089`.
- However, if that DB read throws, the code logs `agent_finally_settle_check_failed` and leaves `already_settled_in_db = False`, then fails open into `_settle_agent_credits(task)` and `_persist_task(...)` at `mariana/agent/loop.py:1071-1102`.
- This is the residual opening that produced the new finding below.

### 3. Local validation / proof of concept
- I built a dedicated local repro in `repro_r01_finally_fetch_failure.py`.
- The repro creates the exact stale-worker / stop-endpoint interleave, then forces the `finally`-block `SELECT credits_settled, state FROM agent_tasks WHERE id = $1` to fail on the worker side.
- Observed result:
  - stop path refunds the full 500 reserved credits first;
  - worker-side `finally` logs `agent_finally_settle_check_failed`;
  - worker then issues a second refund of 420 credits from stale in-memory `spent_usd=0.80`;
  - the final `_persist_task(...)` is blocked by CAS, so the row stays `state=cancelled`, `spent_usd=0.0`, `credits_settled=True`.
- Net effect: the database row looks correct, but two `add_credits` RPCs have already fired.

### 4. Additional auth / conversation / storage / websocket / billing / RLS sweep

#### Authentication / session security
- `_authenticate_supabase_token` now verifies bearer tokens with Supabase Auth via `GET /auth/v1/user` instead of trusting decoded JWT claims at `mariana/api.py:1216-1255`.
- `_get_current_user` rejects missing/empty bearer tokens at `mariana/api.py:1258-1267`.
- I did not find a new auth bypass in this pass.

#### Conversation endpoints
- `GET /api/conversations/{conversation_id}` filters by both `id` and authenticated `user_id` before reading messages at `mariana/api.py:2493-2521`.
- `POST /api/conversations/messages` validates UUID format and verifies ownership before inserting a message at `mariana/api.py:2673-2698`.
- I did not confirm a new cross-user conversation IDOR.

#### Research task ownership / SSE tokens
- Research-task ownership checks use relational `user_id` and only fall back to legacy metadata where needed at `mariana/api.py:1298-1312`.
- Stream tokens are task-bound, HMAC-signed, and short-lived at `mariana/api.py:1378-1457` and `mariana/agent/api_routes.py:595-643`.
- The raw-JWT SSE fallback remains present in agent routes, but the endpoint still loads the task and enforces `task.user_id == current_user["user_id"]`, so I did not promote it as a new finding.

#### Preview / artifact storage
- Preview access is owner-gated through preview cookie / preview token / bearer fallback at `mariana/api.py:1655-1750`.
- Preview cookies are `HttpOnly`, `Secure`, path-scoped to `/preview/{task_id}`, and `SameSite=Lax` at `mariana/api.py:1752-1762`.
- Agent artifacts are exposed only after loading the task and checking `task.user_id` in `mariana/agent/api_routes.py:875-889`.
- `agent_events` has no DB-level RLS in `mariana/agent/schema.sql:61-71`, but all reviewed API reads first verify task ownership; I found no new externally reachable leak from that alone.

#### Billing portal / Stripe / profile patch helpers
- `/api/billing/portal` resolves the Stripe customer from the authenticated `user_id` via `_get_stripe_customer_id(user_id, cfg)` and does not accept caller-supplied `customer_id` at `mariana/api.py:5757-5796` and `mariana/api.py:7253-7282`.
- `invoice.paid` grants credits by event id and patches the matching customer profile at `mariana/api.py:5954-6015`.
- `_supabase_patch_profile(...)` and `_supabase_patch_profile_by_customer(...)` use backend RPCs, not user-controlled direct profile mass assignment, at `mariana/api.py:6832-6902`.
- I re-checked the related RPC privileges below; no new public-callable billing RPC was found.

#### RPC privilege / migration re-check
- Migration `frontend/supabase/migrations/005_loop6_b01_revoke_anon_rpcs.sql` revokes `add_credits`, `deduct_credits`, `get_stripe_customer_id`, `update_profile_by_id`, and `update_profile_by_stripe_customer` from `PUBLIC`, `anon`, and `authenticated`, and grants them only to `service_role` at lines 96-156.
- Migration `frontend/supabase/migrations/021_k02_atomic_charge_reversal.sql` revokes `process_charge_reversal(...)` from `PUBLIC`, `anon`, and `authenticated`, then grants only `service_role` at lines 148-161.
- I did not find a privilege regression in the 020/021-family billing RPC protections.

#### Vault BYOK / secret handling
- `validate_vault_env`, task-scoped Redis storage, context-local secret injection, outbound redaction, and terminal cleanup are implemented in `mariana/vault/runtime.py:59-223` and `mariana/agent/loop.py:1110-1115`.
- Agent event emission and stored step results run through redaction in `mariana/agent/loop.py:236-246` and `mariana/agent/loop.py:541-562`.
- I did not find a new plaintext secret leak in the reviewed code paths.

## Findings

### R-01 — fail-open `finally` settlement check still allows duplicate refund mint when the guard read throws
- Severity: **P1**
- Surface: **agent billing / cancel race / error path in `run_agent_task` finally block**
- Root cause: `mariana/agent/loop.py:1054-1102`

#### Why this is new
Q-01 fixed the stale-persist clobber by tightening the `_persist_task` CAS predicate and by skipping settlement/persist when the `finally` re-read observes `credits_settled=TRUE`. But that defense is conditional on the re-read succeeding. On any exception in the `SELECT credits_settled, state FROM agent_tasks WHERE id = $1`, the code logs and continues with `already_settled_in_db=False`, which re-enables stale settlement from the worker snapshot.

#### Exploit / impact
A realistic interleave is:
1. Worker loads a stale pre-cancel task snapshot with `credits_settled=False`.
2. Stop endpoint wins the row lock, marks the task `CANCELLED`, calls `_settle_agent_credits`, and persists `credits_settled=True` with the canonical settled row.
3. Worker enters `finally:`.
4. The guard read at `loop.py:1058-1062` fails transiently (DB hiccup, connection issue, pool/transaction error, mocked/test failure, or any other exception path).
5. Because the code fails open, worker still calls `_settle_agent_credits(task)` with stale in-memory `spent_usd` and issues a second `add_credits` refund RPC.
6. The trailing `_persist_task(...)` is then blocked by the Q-01 CAS, so the database row remains apparently correct and hides the extra refund.

This mints credits without leaving the canonical `agent_tasks` row in a visibly inconsistent state. The row can remain `cancelled / spent_usd=0 / credits_settled=true` while the ledger has already credited the user twice.

#### Concrete validation
The local repro `repro_r01_finally_fetch_failure.py` produced:
- first refund RPC: `add_credits(..., 500)` from the stop path;
- forced `agent_finally_settle_check_failed` on the worker;
- second refund RPC: `add_credits(..., 420)` from stale worker settlement;
- final DB row: `state=cancelled`, `spent_usd=0.0`, `credits_settled=True`.

So the Q-01 CAS successfully blocks stale persistence, but the credit mint has already happened before that guard is reached.

#### Fix sketch
Use fail-closed semantics for the `finally` guard:
- if the DB re-read throws, do **not** call `_settle_agent_credits(task)` from the stale in-memory snapshot;
- either abort settlement entirely and rely on offline reconciliation, or reload-and-settle only from a fresh locked DB row;
- alternatively, move settlement idempotency into a single DB-side primitive keyed by `task_id` so settlement cannot be replayed from process-local state even when reads fail.

Minimum safe patch: in `run_agent_task(...).finally`, if the `credits_settled/state` fetch fails, skip both settlement and trailing persist instead of treating the row as unsettled.

## No other new findings confirmed in this pass
- Auth middleware / bearer validation: reviewed and no new bypass confirmed.
- Conversation/message endpoints: reviewed and no new user-to-user leak confirmed.
- Preview/artifact access: owner checks and token scoping looked sound in reviewed paths.
- Billing portal and customer lookup: no user-controlled `customer_id` path found.
- Webhook privilege model and RPC grants/revokes: re-checked and no new public-callable billing RPC found.
- Vault BYOK runtime: reviewed for plaintext logging/persistence and did not confirm a new leak.
- `agent_events` lacks DB RLS, but I did not find a new externally reachable read path beyond the existing owner-gated routes.

RE-AUDIT #15 COMPLETE findings=1 file=loop6_audit/A20_phase_e_reaudit.md