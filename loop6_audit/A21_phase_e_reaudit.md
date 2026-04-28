# Phase E re-audit #16

Header: model=claude_opus_4_7, commit=9f80fb3, scope=`fa6cd55..9f80fb3` changed
files (R-01 fix) plus broader sweep across agent billing, ref_id wiring,
PostgREST RPC contract, schema bootstrap, and A20-listed surfaces (Stripe,
agent_events privacy, RLS, credit_buckets, storage, refund authz, JWT
rotation, conversation race).

## Surface walkthrough

### 1. Required reading and diff scope

- Re-read `loop6_audit/REGISTRY.md`, `loop6_audit/R01_FIX_REPORT.md`, and
  `loop6_audit/A20_phase_e_reaudit.md`.
- Walked every file changed in `fa6cd55..9f80fb3`:
  `mariana/agent/loop.py`, `mariana/agent/api_routes.py`,
  `mariana/agent/schema.sql`, `tests/test_r01_settlement_idempotency.py`,
  plus the audit notes and registry update.
- Cross-checked R-01's RPC payload contract against the Supabase function
  catalog via the live `afnbtbeayfkwznhzafay` project and against the
  migration history under `frontend/supabase/migrations/`.

### 2. R-01 fix probes

#### 2.1 Schema bootstrap order
`init_schema` reads `_SCHEMA_SQL` then loads `mariana/agent/schema.sql`
verbatim in one `conn.execute(...)` (`mariana/data/db.py:610-625`). The
new `agent_settlements` table sits at the bottom of `schema.sql:81-93`,
*after* `agent_tasks` (FK target) and `agent_events`. No bootstrap
ordering issue.

#### 2.2 Claim race (probe 1)
`_claim_settlement(...)` in `mariana/agent/loop.py:364-404` runs
`INSERT ... ON CONFLICT (task_id) DO NOTHING RETURNING task_id` inside an
`async with db.acquire() as conn:` block. Under PostgreSQL's
default-read-committed isolation, ON CONFLICT DO NOTHING is atomic at the
row level — the loser blocks until the winner commits, then sees the row
present and skips, returning zero rows. The Python helper returns
`row is not None`, so exactly one caller wins. ✓

#### 2.3 Partial-completion (probe 2)
After a successful `_claim_settlement`, if the subsequent ledger RPC
*raises* (httpx network error etc.), control jumps to `loop.py:630-642`:
the handler sets `task.credits_settled = True` and *returns without
removing the claim row*. The trailing `_mark_settlement_completed` is
guarded by `if db is not None and rpc_succeeded:` (line 644), so on
exception the claim row stays with `completed_at IS NULL` forever.

A retry from anywhere else (worker finally, stop endpoint, requeue) will
hit the existing claim row, log `agent_settlement_already_claimed`, and
*skip* the RPC. This is the documented "operator reconciliation surface"
and is the foundation of finding **S-01** below — it works for one bug
class (transient network errors) but compounds catastrophically with the
ref_id contract bug.

#### 2.4 ref_id collision with prior unrelated transaction (probe 3)
Format is `agent_settle:<uuid>`, so collision with a Stripe event id
(`evt_*`) or research task id is impossible. Verified.

#### 2.5 reserved_credits=0 case (probe 4)
`if task.credits_settled or task.reserved_credits <= 0: return` at
`loop.py:457-458` short-circuits *before* `_claim_settlement`. No claim
row is inserted; no RPC fires. ✓ (note: if reserved_credits is somehow
negative this is also a no-op.)

#### 2.6 delta=0 case (probe 5)
At `loop.py:529-547` after winning the claim, if `delta == 0` the helper
flips `credits_settled = True`, marks `completed_at`, and returns without
calling any RPC. ✓

#### 2.7 Negative reserved/final_credits (probe 6)
`agent_settlements` schema declares `reserved_credits BIGINT NOT NULL`
and `final_credits BIGINT NOT NULL` *without* CHECK constraints
(`schema.sql:81-90`). A hostile or buggy state passing
`final_credits<0` would persist unchecked. Not directly exploitable —
the helper computes `final_tokens = int(task.spent_usd * 100)` and
spent_usd is bounded ≥0 by accounting code paths — but it's a missing
defense-in-depth opportunity.

#### 2.8 FK ON DELETE CASCADE (probe 7)
`agent_settlements.task_id REFERENCES agent_tasks(id) ON DELETE CASCADE`
(`schema.sql:82`). Deleting an agent_task wipes its settlement claim. If
a task UUID is later reused (via test/admin tooling that re-inserts the
same UUID), settlement could be claimed again, allowing a duplicate
ledger move. UUID collision is improbable, but admin re-insert with a
captured UUID is a residual risk. Tagged as low-severity finding **S-04**
defense-in-depth below.

#### 2.9 Stale snapshot's spent_usd (probe 8)
`final_tokens = int(task.spent_usd * 100)` at `loop.py:481` reads from
the *in-memory* `task.spent_usd`, not from a fresh DB read. The claim
row records that exact value, so future reconciliation sees what the
worker thought it owed. This is consistent with the existing
P-01/Q-01/R-01 model — settlement is computed from whichever caller
wins the claim. Not new.

#### 2.10 Operator reconciliation path (probe 9)
The partial index `idx_agent_settlements_completed ON agent_settlements
(completed_at) WHERE completed_at IS NULL` (`schema.sql:92-93`) makes
finding stuck rows efficient. There is **no** cron, background job, or
scripted reconciler in this branch. R01_FIX_REPORT.md and the inline
comments only refer to "operator reconciliation surface" — manual,
undocumented playbook. Combined with finding **S-01**, this means once
the broken RPC contract is hit in production, every agent settlement
will silently stall and require manual SQL intervention; users will
notice missing refunds but operators will not have automatic alerting.

#### 2.11 Stop endpoint + worker race new behavior (probe 10)
`mariana/agent/api_routes.py:849` passes `db=db` into
`_settle_agent_credits`, so both stop endpoint and worker finally use
the new claim-row primitive. Whichever lands first wins; the loser
short-circuits on `won=False`. The pre-R01 race window is closed. ✓

#### 2.12 Backward compat for pre-R-01 settled tasks
A task settled before this commit deployed has `credits_settled=TRUE`
on `agent_tasks` but NO row in `agent_settlements`. If anything causes
`_settle_agent_credits` to be called on such a task (admin retry,
requeue), the early `if task.credits_settled` guard at `loop.py:457`
short-circuits before the claim — no double settle. ✓

#### 2.13 Test coverage
`tests/test_r01_settlement_idempotency.py` mocks `httpx.AsyncClient` via
`_ScriptedClient` (`tests/test_r01_settlement_idempotency.py:136-160`).
This client returns `status=200` regardless of payload shape and never
exercises the actual PostgREST endpoint. The contract bug in S-01 is
not caught.

The file's `test_r01_settlement_table_records_outcome` test does
verify the `ref_id` column on `agent_settlements` matches
`agent_settle:{task.id}` (line 341), but it does NOT verify the RPC
payload reaches Supabase intact, nor that the function signature
accepts the supplied keys.

### 3. Live-PostgREST contract verification

I issued live `curl` calls against `https://afnbtbeayfkwznhzafay.supabase.co`
to validate the R-01 RPC payload shape. Evidence saved at
`loop6_audit/A21_evidence_add_credits_404.txt` and
`loop6_audit/A21_evidence_deduct_credits_404.txt`.

`pg_proc` lookup confirms the function catalog only has 2-arg overloads
(`SELECT proname, pg_get_function_arguments(oid)` on `add_credits`,
`deduct_credits`):

```
add_credits(p_user_id uuid, p_credits integer)        → void
deduct_credits(target_user_id uuid, amount integer)   → integer
```

R-01's payload sends a third JSON key `ref_id`. PostgREST returns:

```
HTTP 404
{"code":"PGRST202",
 "message":"Could not find the function public.add_credits(p_credits, p_user_id, ref_id) in the schema cache",
 "hint":"Perhaps you meant to call the function public.add_credits(p_credits, p_user_id)"}
```

(identical for deduct_credits with `target_user_id, amount, ref_id`).

Per Supabase docs, PGRST202 indicates "stale function signature" — the
schema cache lookup uses the named-argument set as a key, and an
overload with `ref_id` does not exist.

### 4. Broader sweep (A20-listed surfaces)

#### Stripe webhook signing (S-02 candidate)
`mariana/api.py` Stripe verification calls `stripe.Webhook.construct_event`
which internally uses `hmac.compare_digest` and enforces a default
`tolerance=300s` skew window. No new finding here.

#### agent_events privacy
`get_agent_events` (`agent/api_routes.py:557-593`) and
`stream_agent_events` (`agent/api_routes.py:647+`) both load the task
first and check `task.user_id != current_user["user_id"]`. Direct DB
queries are gated. The lack of DB-level RLS was already noted in A20 and
is not a new finding.

#### credit_buckets RLS
Already covered by I-03/B-12 marker tables and migrations 011/019.

#### Storage policies for agent artifacts
ADR-B42 + migration 016. Already audited.

#### Vault / 2FA / password reset
No reset/2FA flow added in this commit range; out of scope for new
finding.

#### Rate limiting on /api/agent
Reservation enforcement happens via Supabase `deduct_credits` so
infinite spam without funds fails. No new finding.

#### Subscription proration
No code change in this range.

#### /api/refund authorization
No admin refund endpoint exists in this branch.

#### /api/conversations race with same client UUID
`POST /api/conversations` uses server-generated UUIDs; client cannot
choose them. Cross-tenant pollution requires UUID collision (≈0).

#### agent_events.payload jsonb injection
Payload is treated as JSON throughout; no `EXECUTE` against it.

#### JWT key rotation
`_authenticate_supabase_token` calls `GET /auth/v1/user` and trusts
Supabase to enforce key rotation. ✓

## Findings

### S-01 — R-01 settlement RPC payload includes `ref_id` key that no `add_credits`/`deduct_credits` overload accepts; every agent settlement RPC returns HTTP 404, refunds permanently stall

- **Severity: P0** (credit-accounting catastrophe, every agent task
  settlement leaks reserved credits)
- **Surface: agent billing / settlement RPC contract / cross-service**
- **Root cause:** `mariana/agent/loop.py:564-568` and
  `mariana/agent/loop.py:600-606`

The R-01 fix added `ref_id` to the JSON body of the Supabase RPC calls:

```python
# loop.py:559-570  (deduct branch)
rpc_url = f"{cfg.SUPABASE_URL}/rest/v1/rpc/deduct_credits"
resp = await client.post(
    rpc_url,
    json={
        "target_user_id": task.user_id,
        "amount": delta,
        "ref_id": ref_id,        # <-- new in R-01, not accepted by RPC
    },
    headers=headers,
)

# loop.py:599-608  (refund branch)
rpc_url = f"{cfg.SUPABASE_URL}/rest/v1/rpc/add_credits"
resp = await client.post(
    rpc_url,
    json={
        "p_user_id": task.user_id,
        "p_credits": refund,
        "ref_id": ref_id,        # <-- new in R-01, not accepted by RPC
    },
    headers=headers,
)
```

The R01_FIX_REPORT motivates this as "ledger-level idempotency as
defense-in-depth", but neither `add_credits` nor `deduct_credits` has a
3-argument overload. PostgREST's named-argument matcher returns
`PGRST202 / HTTP 404` (`Could not find the function ... in the schema
cache`).

#### Live evidence

`pg_proc` lookup against `afnbtbeayfkwznhzafay`:

```
add_credits(p_user_id uuid, p_credits integer)        → void
deduct_credits(target_user_id uuid, amount integer)   → integer
```

`curl` against `/rest/v1/rpc/add_credits` with the R-01 payload returns:

```
HTTP 404
{"code":"PGRST202",
 "details":"Searched for the function public.add_credits with parameters
   p_credits, p_user_id, ref_id ... but no matches were found",
 "hint":"Perhaps you meant to call the function public.add_credits(p_credits, p_user_id)"}
```

Stored in `loop6_audit/A21_evidence_add_credits_404.txt` and
`loop6_audit/A21_evidence_deduct_credits_404.txt`.

#### Why R-01 tests miss this

`tests/test_r01_settlement_idempotency.py` injects a `_ScriptedClient`
(line 136) that records POST bodies and *unconditionally returns
`status=200`*. The tests assert internal recording behaviour, not that
the JSON shape is acceptable to PostgREST. The pre-R01
`mariana/main.py:_deduct_user_credits` and the original
`mariana/agent/loop.py` both used 2-key payloads, so this regression is
purely R-01-introduced.

#### Exploit / impact

For any user task that completes:

1. Worker calls `_settle_agent_credits(task, db=db)`.
2. `_claim_settlement` succeeds — claim row inserted.
3. Branch `delta != 0`: `httpx.AsyncClient.post(...)` fires.
4. PostgREST returns 404. `resp.status_code` is 404, not in (200, 204).
5. Code path reaches `loop.py:586` (deduct) or `loop.py:620` (refund),
   logging `agent_credits_settle_*_failed` — but `task.credits_settled
   = True` was already set on lines 575/609 *before* the status check.
6. `rpc_succeeded` stays `False`. `_mark_settlement_completed` is
   *not* called. Claim row sits with `completed_at IS NULL`.
7. Trailing `_persist_task` writes `credits_settled=TRUE` to the row.
8. Net result: ledger never moves. The user's reserved credits are
   permanently held against a `credit_clawback` (or equivalent) and
   never refunded. For overrun tasks, the platform never bills.

Per task this is on the order of $0.01–$5.00 (the budget cap). At any
deployed scale, this represents continuous, silent credit leakage AND
silent revenue leakage, with the canonical `agent_tasks` row appearing
fully settled.

The "operator reconciliation surface" (the partial index on
`completed_at IS NULL`) does identify the stuck rows but no automatic
job retries them. Even if it did, retrying the same broken payload
returns 404 again — the bug is in the call shape, not the network.

Worse, the in-memory `credits_settled=True` flag *also* gets persisted
to `agent_tasks` via `_persist_task`, so subsequent calls will hit the
early `if task.credits_settled: return` short-circuit and never even
attempt to retry. This couples the regression with the existing
"in-memory-flag-as-idempotency" path that R-01 was supposed to demote.

#### Fix sketch

Two sound options:

1. **Drop `ref_id` from the RPC payload** — revert the new key on lines
   567 and 605. The `agent_settlements` claim row already provides
   sufficient idempotency at the orchestrator side. Ledger-side
   defense-in-depth was a "nice to have" and is currently a fatal
   contract mismatch.

2. **Add a 3-arg overload at the ledger** — write a migration
   `022_r01_add_credits_with_ref.sql` creating
   `add_credits(p_user_id uuid, p_credits integer, p_ref_id text)` and
   `deduct_credits(target_user_id uuid, amount integer, p_ref_id text)`,
   enforcing `(ref_type='agent_settle', ref_id)` idempotency at
   `credit_transactions` (similar to existing `grant_credits` /
   `refund_credits` patterns). Update orchestrator JSON keys to
   `p_ref_id` to match the named-arg PostgREST contract. Revoke the new
   overloads from `anon`/`authenticated` and grant only `service_role`
   (mirrors migration 005).

   Note: PostgREST's named-arg lookup means the *key names in the JSON
   body* must match the function's argument names exactly. So even
   option 2 needs the orchestrator to send `p_ref_id`, not `ref_id`.

3. **Add a contract regression test** that performs a real (or
   stub-PostgREST) signature check on every RPC payload the orchestrator
   issues, parameterised over the migration history. This catches future
   add/remove of named arguments on either side.

Recommended: option 1 (immediate revert) plus option 3 (regression),
since option 2 also requires reconciling the existing ledger
idempotency tables and is a larger surface change.

### S-02 — `agent_settlements` lacks CHECK constraints on credit columns; hostile or corrupted state can persist negative `final_credits`/`reserved_credits`

- **Severity: P3** (defense-in-depth)
- **Surface: db/schema/agent_settlements**
- **Root cause:** `mariana/agent/schema.sql:81-90`

```sql
CREATE TABLE IF NOT EXISTS agent_settlements (
    task_id           UUID PRIMARY KEY REFERENCES agent_tasks(id) ON DELETE CASCADE,
    user_id           TEXT NOT NULL,
    reserved_credits  BIGINT NOT NULL,
    final_credits     BIGINT NOT NULL,
    delta_credits     BIGINT NOT NULL,
    ref_id            TEXT NOT NULL,
    claimed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at      TIMESTAMPTZ
);
```

Compare to `credit_transactions`/`credit_clawbacks` which assert
non-negative columns. If any code path persists a negative value here,
the table accepts it silently. Not directly exploitable today (the
helper computes `final_tokens = int(task.spent_usd * 100)` from a
non-negative spent_usd, and `reserved_credits` is taken from
`agent_tasks` whose default is 0), but a future refactor or admin path
could introduce drift that goes unnoticed.

#### Fix sketch
Add CHECK constraints:
```sql
CHECK (reserved_credits >= 0),
CHECK (final_credits >= 0)
-- delta_credits is signed (overrun/refund); leave it unconstrained.
```

### S-03 — No automatic reconciler for `agent_settlements` rows where `completed_at IS NULL`

- **Severity: P2** (operational, amplifies S-01)
- **Surface: agent billing / ops**
- **Root cause:** absence of background job; documented only as "operator reconciliation surface" in `loop.py:573-574`, `loop.py:633`, and `R01_FIX_REPORT.md:117`.

The partial index `idx_agent_settlements_completed` lets ops *find*
stuck rows, but nothing retries them. Combined with S-01 this means
every settlement attempt produces a stuck row with no automated rescue.
Even after S-01 is fixed, transient Supabase 5xx or pool exhaustion
would still leave stuck rows that require manual intervention.

#### Fix sketch
Add an asyncio task in `mariana/main.py` that periodically scans
`agent_settlements WHERE completed_at IS NULL AND claimed_at < now() -
interval '5 minutes'`, re-issues the appropriate RPC using the row's
recorded `delta_credits`/`ref_id`, and stamps `completed_at` on success.
The retried RPC must use the row's stored `ref_id` for idempotency at
the ledger.

### S-04 — `agent_settlements ON DELETE CASCADE` permits double-settle if an `agent_tasks` row is deleted and a row with the same UUID is later re-inserted

- **Severity: P4** (low likelihood, requires admin/test tooling)
- **Surface: db/schema**
- **Root cause:** `mariana/agent/schema.sql:82` — `REFERENCES
  agent_tasks(id) ON DELETE CASCADE`

Deletion of an `agent_tasks` row also wipes its `agent_settlements`
claim. If admin tooling reuses the original UUID (uncommon but possible
in fixtures, soft-delete restoration, or after a B-tree corruption
rebuild), `_claim_settlement` will succeed again and a fresh ledger RPC
will fire, double-counting the original transaction.

#### Fix sketch
Either change to `ON DELETE RESTRICT` (forces ops to acknowledge the
settlement before deleting the task), or keep the cascade but record
deletions in a separate `agent_settlements_history` table so a future
re-insert can detect a prior settlement.

## No other new findings confirmed in this pass

- Stripe webhook signing/replay: covered by `stripe-python`'s
  `compare_digest` and `tolerance` defaults.
- `agent_events` privacy: all read paths gated by task ownership.
- credit_buckets RLS: unchanged from migration 014; no regression.
- Storage policies: unchanged from migration 016.
- `/api/agent` rate limiting: gated by `deduct_credits` reservation;
  spam without funds is rejected.
- Subscription proration / `/api/refund` admin: unchanged in this commit.
- `/api/conversations` UUID race: server-generated UUIDs, no client
  control.
- agent_events.payload JSONB injection: no `EXECUTE` of user JSON.
- JWT key rotation: enforced by Supabase auth via `GET /auth/v1/user`.
- Schema bootstrap: `init_schema` executes `agent/schema.sql` whole;
  `agent_settlements` defined after FK target.
- Backward-compat re-settle of pre-R01 rows: short-circuited by
  `task.credits_settled` early guard.

RE-AUDIT #16 COMPLETE findings=4 file=loop6_audit/A21_phase_e_reaudit.md
