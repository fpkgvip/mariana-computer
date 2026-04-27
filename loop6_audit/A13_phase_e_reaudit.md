# A13 — Phase E re-audit #8

## Executive summary

I found **0 new issues** that the prior seven re-audits (A6 through A12) missed.

Read-only audit performed against branch `loop6/zero-bug` at commit `05299a5`,
with live cross-reference against Supabase project `afnbtbeayfkwznhzafay`. Every
finding raised by re-audits #1 through #7 (F-01..F-06, G-01, H-01..H-02,
I-01..I-03, J-01..J-02, K-01..K-02, L-01) is fixed and verified. I attempted
to find additional bugs in the surfaces those audits stressed, in adjacent
surfaces they covered lightly, and in repo-wide sanity checks. None of the
candidates promoted past MEDIUM-LOW confidence — all turned out to be either
defense-in-depth that already had another guard, or known/intentional design
decisions documented in the code itself.

If round #9 and round #10 also find zero bugs, convergence is achieved.

---

## Methodology

I prioritised the surfaces most likely to harbour a missed bug:

1. **The L-01 fix itself** (commit `05299a5`) — a fresh fix is the highest-yield
   place to look for a missed bug. Line-by-line review of `_grant_credits_for_event`
   plus its callers and downstream consumers (`_lookup_grant_tx_for_payment_intent`,
   `_reverse_credits_for_charge`, `process_charge_reversal`).
2. **The two atomic-RPC migrations** (`020`, `021`) plus the existing
   ledger RPCs (`grant_credits`, `refund_credits`, `add_credits`,
   `spend_credits`, `expire_credits`, `admin_set_credits`,
   `admin_adjust_credits`).
3. **Live database state** for privilege drift, NULL-policy gaps, and
   missing `SET search_path` on SECURITY DEFINER routines.
4. **Lock-ordering and serialization** — every advisory-lock keyspace in
   the codebase, looking for a deadlock cycle or a TOCTOU window the
   per-charge lock added in `021` does not cover.
5. **Webhook claim/finalize machinery** — the two-phase `_claim_webhook_event`
   path and its interaction with concurrent same-event deliveries.
6. **Standard surfaces** — `mariana/billing/ledger.py`, the F-01/F-02
   preview surface, the file upload routes, admin REST proxy, and the
   frontend auth/api stack.
7. **Repo-wide sanity checks** — unauthenticated mutating routes, X-Forwarded-For
   trust, SQL injection via PostgREST URL parameters, and Stripe-signature
   verification.

For each candidate I asked: is this exploitable? Is the impact real money or
real authorization? Is the confidence at least MEDIUM-HIGH? If the answer to
any was "no", I deliberately discarded it rather than report it as noise.

---

## Hot spots reviewed with no new finding

### 1. The L-01 fix (mariana/api.py:6084-6204, commit 05299a5)

The fix unconditionally attempts the `stripe_payment_grants` insert when
`pi_id` is provided, regardless of whether `grant_credits` returned `granted`
or `duplicate`, and treats both transport exceptions and non-2xx HTTP
responses as fatal — raising `HTTPException(503, "Credit grant mapping failed")`
([mariana/api.py:6176-6204](mariana/api.py)).

I traced four ways this could still leak money:

- **Stripe gives up on retries before mapping ever lands.** Stripe's
  documented retry window is 3 days with exponential backoff. In the
  pathological scenario where every retry fails the mapping insert for the
  full 3 days, the grant is permanent but the mapping row is missing. This
  is a deployment-tier concern, not a code bug — and the code surfaces a
  503 every time, which alerts on-call. Acceptable trade-off; the prior
  "silent skip on duplicate" path was the actual bug.
- **Cross-account contamination via stripe_payment_grants PK.** The PK is
  `payment_intent_id` ([017_h01_h02_stripe_grant_linkage.sql:14](frontend/supabase/migrations/017_h01_h02_stripe_grant_linkage.sql)).
  The `Prefer: resolution=ignore-duplicates,return=minimal` header at line 6156 means
  retries are safe; existing rows are returned as 2xx with empty body. Two
  different PaymentIntents can never collide.
- **Order of inserts matters under partial failure.** `grant_credits` writes
  the canonical credit row first; if its retry returns `status='duplicate'`
  on a future delivery, the user did not get double-credited. The mapping
  insert is downstream of that, idempotent on PK, so a retry of the same
  event can heal the missing mapping row. Confirmed by reading
  `tests/test_l01_mapping_insert_failure.py`.
- **HTTP status 200/201/204 set vs PostgREST behaviour.** PostgREST returns
  `201 Created` when the row was inserted and `200 OK` (with empty body, due
  to `return=minimal`) when `ignore-duplicates` collapsed onto an existing
  row. The fix accepts both plus 204. No status code that PostgREST emits
  on success is missing.

### 2. K-01 (charge_amount column, migration 020)

`stripe_payment_grants.charge_amount` is nullable to permit legacy rows. The
live project has zero rows in `stripe_payment_grants` today, so there are no
legacy rows where the K-01 dispute pro-rata fallback would silently
over-debit. Every grant path now writes `charge_amount`:

- `_handle_checkout_completed`: `session.amount_total` ([api.py:5911](mariana/api.py))
- `_handle_invoice_paid`: `invoice.amount_paid` ([api.py:5992](mariana/api.py))
- `_handle_payment_intent_succeeded`: `pi.amount` ([api.py:6056](mariana/api.py))

`_lookup_grant_tx_for_payment_intent` reads it back ([api.py:6358](mariana/api.py));
`_reverse_credits_for_charge` overrides `amount_total` from the stored value
on the dispute path ([api.py:6628-6639](mariana/api.py)). The legacy path
emits `charge_reversal_dispute_legacy_grant_no_charge_amount` and falls
through to the "full reversal" branch — conservative (over-debits the user
in their favour) but not exploitable for money leak.

### 3. K-02 (atomic per-charge reversal, migration 021)

`process_charge_reversal` is a SECURITY DEFINER PL/pgSQL function granted
only to `service_role`, with explicit `SET search_path = public, pg_temp`.
I verified each correctness property:

- The per-charge advisory lock keyspace `hashtextextended('charge:' || p_charge_id, 0)`
  cannot collide with the existing per-user keyspace
  `hashtextextended(p_user_id::text, 0)` because UUIDs do not start with
  `charge:`.
- Lock acquisition order is **charge → user** (the user lock is taken
  inside `refund_credits`, which runs after the charge lock). I grepped
  for any path that takes user-then-charge and found none. No deadlock
  cycle.
- The dedup-row INSERT happens before `refund_credits` is called, but both
  run in the same transaction. If `refund_credits` raises, the dedup row
  rolls back too. Stripe-retry safe.
- `refund_credits` does **not** raise on insufficient balance — it
  records a `credit_clawbacks` deficit row and returns
  `status='deficit_recorded'` ([009_f03_refund_debt.sql:185-190](frontend/supabase/migrations/009_f03_refund_debt.sql)).
  `process_charge_reversal` propagates that as the inner `refund_result`
  field; the outer Python wrapper logs it but does not fail the webhook.
  No double-debit possible because the dedup row pins this reversal_key.
- Privileges are explicitly revoked from `PUBLIC`, `anon`, and
  `authenticated`. Migration includes a `DO $post$` block that asserts
  no hostile EXECUTE grants remain.

### 4. Webhook claim/finalize concurrency

`_claim_webhook_event` ([api.py:7135-7196](mariana/api.py)) uses a CTE that
captures `prior_status` from the row before the upsert and `post_status`
from the upserted row. Two concurrent deliveries with the same event_id
serialize at the unique constraint on `event_id`, but the `prior` SELECT in
the second caller's CTE may still see no row because its statement snapshot
predates the first caller's commit. In that case, both callers think they
are `NEW` and both run the handler concurrently.

This is **defensively safe** because every downstream operation has its own
idempotency layer:

- `grant_credits` is idempotent on `(ref_type='stripe_event', ref_id=event_id)`
  via `uq_credit_tx_idem` (described inline in
  [009_f03_refund_debt.sql](frontend/supabase/migrations/009_f03_refund_debt.sql)
  comments). The second handler's grant returns `status='duplicate'`.
- `stripe_payment_grants` is unique on `payment_intent_id` (PK) and the
  insert uses `Prefer: ignore-duplicates`, so the second handler's
  mapping insert collapses onto the first's row.
- `process_charge_reversal` takes a per-charge advisory lock and dedups
  on `reversal_key` inside the lock, so two concurrent reversals
  serialize and the second sees the first's dedup row.

The race window cannot produce double-credit, double-debit, or missing-mapping
outcomes. Reporting this as a finding would be false-positive noise: the
documented design is "rerun is safe, downstream guards catch double-execute",
and it actually does.

### 5. Live database privilege posture (project afnbtbeayfkwznhzafay)

I queried `information_schema.routine_privileges` and confirmed:

- Only `check_profile_immutable` and `is_admin` are EXECUTE-granted to
  `anon` or `PUBLIC`. Both are pure-read functions returning boolean and
  cannot mutate state or leak data beyond a single boolean.
- All credit RPCs (`grant_credits`, `refund_credits`, `add_credits`,
  `spend_credits`, `expire_credits`, `admin_set_credits`,
  `admin_adjust_credits`, `admin_*` mutators, `process_charge_reversal`)
  are revoked from `anon`/`authenticated`/`PUBLIC` and granted only to
  `service_role`.
- Every SECURITY DEFINER function has an explicit `SET search_path`. None
  is missing the clause. `admin_set_credits` uses `''` (empty), the
  safest setting; the rest use `public, pg_temp` or `public, auth`. None
  is exploitable via a search-path hijack.

### 6. Unauthenticated mutating routes

The only unauthenticated mutating HTTP endpoint is `POST /api/stripe/webhook`,
which verifies the Stripe signature before any side-effect (event["type"]
read) ([api.py:5621-5645](mariana/api.py)). Public GETs (`/api/health`,
`/api/plans`, `/api/orchestrator-models`) do not mutate state and do not
read tenant data.

The preview routes (`/preview/{task_id}`, `/preview/{task_id}/{file_path:path}`,
`/api/preview/{task_id}`) all owner-gate via `_authorize_preview_request`
or `_get_current_user`. The path validator `_SAFE_PREVIEW_TASK = ^[A-Za-z0-9_\-]{1,64}$`
rejects anything that could escape `_PREVIEW_ROOT_PATH`. The `file_path:path`
parameter goes through both an explicit `..`/`\x00` reject and a
`target.relative_to(root)` check after `.resolve()`.

### 7. File upload routes (F-01/F-02 surface)

`POST /api/investigations/{task_id}/upload` and `POST /api/upload`
([api.py:4740-5012](mariana/api.py)) implement:

- UUID validation on `task_id` and `session_uuid` (rejects path-traversal
  values before any `Path()` composition).
- Per-task / per-session async lock to serialize the count-check, ownership-bind,
  and file-write phases (closing the TOCTOU window).
- Owner-bind via `os.open(O_CREAT|O_EXCL)` on the `.owner` file, which is
  atomic across processes sharing the filesystem.
- Streaming chunked read with size cap; oversized uploads are rejected
  before being fully buffered.
- Filename sanitisation `re.sub(r"[^\w\-.]", "_", filename)` followed by
  `os.path.basename(...)`, dotfile rejection, and a defence-in-depth
  `dest.resolve().startswith(upload_dir.resolve())` check. Because
  `safe_name` cannot contain `/` after sanitisation+basename, the
  resolve check is redundant but not buggy. (No, the
  `"/x/y2".startswith("/x/y")` false-positive case cannot occur because
  `safe_name` is bound to a single basename and never contains a path
  separator.)
- Symlink reject via `dest.is_symlink()` after write — race-safe because
  the per-task lock holds and a malicious actor would need filesystem
  write access to plant the symlink.

### 8. Admin REST proxy (`_admin_rest_request`, `_admin_rpc_call`)

PostgREST URL params for `/api/admin/admin-tasks` filter use Pydantic
`Query(...)` constraints (`max_length=64`, etc.) but no value-content
whitelist. I considered whether a malicious admin (already privileged)
could inject extra PostgREST operators by including `,` or `&` or `:`
in the value. `httpx`'s `params=` argument percent-encodes values when
serialising the query string, so PostgREST sees the literal value. Even
if it somehow didn't, the actor is already an admin per
`_require_admin`, so the marginal impact is zero. Not a bug.

`_admin_supabase_headers` forwards the caller's user JWT in
`Authorization` and uses the service-role key as the `apikey` API-gateway
credential. This is the documented pattern for PostgREST under the loop6
B-01 partial-revoke posture; the user JWT remains the source of truth
for `auth.uid()` inside SECURITY DEFINER functions.

### 9. mariana/billing/ledger.py

`_rpc()` accepts only `200` (line 74). All credit RPCs return `jsonb`,
which always emits 200 with body — never 204. No incompatibility. The
`get_balance` view query uses `eq.{user_id}` with a service-role-authenticated
context where `user_id` is a UUID derived from the caller's JWT in upstream
code. No injection vector.

### 10. Frontend `lib/api.ts` and `contexts/AuthContext.tsx`

`apiRequest` always re-fetches the Supabase access token before each call,
attaches it as `Authorization: Bearer`, and uses `credentials: "same-origin"`
so the F-01 preview cookie travels with same-origin manifest requests
without leaking elsewhere.

`AuthContext` initializes via `onAuthStateChange` only (avoids the
double-fetch race documented as BUG-008/BUG-R2C-11), retries `fetchProfile`
up to 5 times with 500 ms delays to tolerate slow profile-trigger fires,
clears the `react-query` cache and dispatches a `deft:logout` event on
sign-out so module-scoped caches in other files (FE-HIGH-02 fix) drop
prior-user data. No new race or stale-data leak.

### 11. Lock-ordering audit (repo-wide)

Searched for every `pg_advisory_xact_lock(...)` usage:

- Per-user: `hashtextextended(p_user_id::text, 0)` — used in
  `grant_credits`, `spend_credits`, `refund_credits`, `add_credits`.
- Per-charge: `hashtextextended('charge:' || p_charge_id, 0)` — used
  only in `process_charge_reversal` (migration 021).

The only path that takes both is `process_charge_reversal`: charge first
(line 76 of migration 021), user second (inside `refund_credits` it
calls). No other path acquires user-then-charge. No deadlock cycle.

Hash-collision probability between the two keyspaces is on the order of
2^-64 per (user_id, charge_id) pair — negligible.

### 12. X-Forwarded-For trust posture

`_audit_or_503` uses `request.client.host` ([api.py:7624](mariana/api.py)),
which is the immediate TCP peer (load balancer or direct client) — **not**
the spoofable `X-Forwarded-For` header. No untrusted-header trust drift
since A12.

### 13. Stripe signature verification

`stripe_webhook` calls `_stripe.Webhook.construct_event(...)` before
inspecting the JSON body. Any signature mismatch raises and returns 400
before any side effect. Replaced-secret handling explicitly emits a
"signature_secret_mismatch" log and 401 with a hint to update the
dashboard ([api.py:5621-5641](mariana/api.py)). The webhook receives
raw body bytes, not a parsed dict, before signature check — correct order.

### 14. SECURITY DEFINER routines added since A12

There are no new SECURITY DEFINER routines since A12. Migrations beyond
`021` do not exist on this branch. I confirmed via
`grep -rn "SECURITY DEFINER" frontend/supabase/migrations/` and matched
the count against the live `pg_proc.prosecdef = true` rows. Every
SECURITY DEFINER function has `SET search_path` set; none is anon-callable
except the two read-only boolean utilities noted above.

---

## Candidates investigated and discarded (LOW/MEDIUM-LOW confidence)

These are explicitly *not* findings — recording them so the next round
can skip the same dead-ends.

- **Preview cookie SameSite=Lax may not flow on cross-origin iframes.**
  This is a UX/functionality concern (third-party-iframe cookie suppression
  in modern browsers) and the code already provides `?preview_token=...`
  query-string and `Authorization` header fallbacks. Not a security or
  correctness bug.
- **`_claim_webhook_event` snapshot race may classify duplicates as NEW.**
  Documented as defense-in-depth and protected by per-RPC idempotency
  downstream. Cannot produce double-credit, double-debit, or missing
  mapping. Reporting it would be noise.
- **`_handle_checkout_completed` `'sub' in dir()` is hacky but correct.**
  Python's `dir()` lists names that have been bound in the current scope;
  if the `_stripe.Subscription.retrieve(...)` succeeded, `sub` is bound.
  No exception path produces a stale `sub`. Style nit, not a bug.
- **Audit log `p_user_agent` length unbounded.** Pydantic does not
  enforce a max length on the User-Agent string before it lands in
  `audit_log`. The likely upper bound is HTTP server limits (Caddy/Nginx
  default ~8 KiB header). Speculative DoS at best, not exploitable.
- **`_admin_rest_request` PostgREST filter values are admin-controlled
  but not value-whitelisted.** httpx percent-encodes params; even a
  malicious admin would only be able to query data they already have
  EXECUTE/RLS access to. Not exploitable.
- **`get_balance` direct view query uses `eq.{user_id}` interpolation.**
  `user_id` is a UUID from authenticated context. No injection surface.

---

## Confidence: HIGH

Confidence that this audit found zero new bugs is HIGH because:

1. The L-01 fix is the only material change since A12. I read it
   line-by-line and traced every retry/replay path through the consumers.
2. I cross-checked the live database privilege state against the migration
   files. No drift.
3. Every advisory-lock keyspace in the repo is either per-user (legacy,
   unchanged) or per-charge (added in 021, audited). No new deadlock cycle.
4. Standard surfaces (ledger.py, frontend, file uploads, admin proxy)
   are unchanged or trivially-changed since prior rounds, and re-reading
   them turned up only style nits and known design trade-offs.
5. Each candidate I considered had at least one downstream guard
   (idempotency, RLS, advisory lock, or owner check) that closed the
   exploit path. None met the MEDIUM-HIGH bar required to report.
