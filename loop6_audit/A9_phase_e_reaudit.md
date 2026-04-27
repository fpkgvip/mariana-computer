# A9 — Phase E re-audit #4

## Executive summary

I found three new issues that were missed by re-audits #1, #2, and #3.

1. **I-01 [P2] credit ledger | `add_credits` lacks the per-user advisory lock that `grant_credits` / `spend_credits` / `refund_credits` use, allowing concurrent calls to silently inflate `profiles.tokens` against open clawbacks**
   - The replaced `add_credits` SQL function (`009_f03_refund_debt.sql:347-409`) reads `v_open_total` from `credit_clawbacks` BEFORE its `FOR UPDATE` clawback-satisfy loop and never calls `pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0))`, while every sibling function in the same migration does take that lock.
   - When `refund_credits` (which inserts a clawback row under the lock) and `add_credits` (no lock) interleave for the same user, `add_credits` can compute `v_net_addition := GREATEST(0, p_credits - v_open_total)` from a stale `v_open_total = 0`, then later satisfy the now-visible clawback in the FOR UPDATE loop AND still add the full pre-clawback `v_net_addition` to `profiles.tokens`.
   - Net result: the user receives both the clawback satisfaction and a full `profiles.tokens` credit, double-crediting them.

2. **I-02 [P2] billing/webhooks | `_record_dispute_reversal_or_skip` is a TOCTOU check-then-insert, allowing concurrent `charge.dispute.created` and `charge.dispute.funds_withdrawn` webhooks to bypass the H-02 dedup**
   - `mariana/api.py:6324-6389` performs `SELECT … reversal_key=eq.<key>` and returns `False` (proceed) if the row is absent.
   - The caller at `mariana/api.py:6446-6568` then runs `_refund_rpc(ref_id=event_id)` BEFORE inserting the dedup row at `_insert_dispute_reversal`.
   - `refund_credits` is idempotent only on `(type='refund', ref_type, ref_id)`, and `event_id` differs between the two dispute events, so it does not collapse them.
   - Two concurrent webhook deliveries (Stripe explicitly does not guarantee ordering, and retries can overlap new events on multi-replica deployments) can both pass the SELECT, both succeed in the refund RPC, and only one of the two `INSERT`s into `stripe_dispute_reversals` is collapsed by the `reversal_key` PRIMARY KEY conflict.
   - Net result: the user is debited twice for the same dispute.

3. **I-03 [P3] DB privilege model | `loop6_007_applied` and `loop6_008_applied` are publicly readable and writable from the browser anon key with RLS disabled**
   - Live audit on project `afnbtbeayfkwznhzafay` shows exactly two public tables with `relrowsecurity = false`: `loop6_007_applied` and `loop6_008_applied`.
   - Both grant `INSERT, SELECT, UPDATE, DELETE, TRUNCATE` to roles `anon` and `authenticated`.
   - Any browser holding the public anon key can `SELECT`, `INSERT`, `DELETE`, or `TRUNCATE` these tables. They contain only migration-applied markers, not PII or credits, so impact is limited, but they are a tampering surface that bypasses the rest of the RLS posture.

I also re-checked the requested hot spots and did **not** find a reportable new issue in the frontend `dangerouslySetInnerHTML` paths (`Chat.tsx`, `FileViewer.tsx`, `chart.tsx`), the Vercel CSP, the Stripe webhook event-coverage gap, the SECURITY DEFINER `search_path` posture (all explicit), or the RLS posture of the `stripe_payment_grants` / `stripe_dispute_reversals` tables added in migration 017.

---

## I-01 [P2] credit ledger | `add_credits` lacks the advisory lock used by sibling ledger functions, double-crediting a user when it interleaves with `refund_credits`

**File(s) + line numbers**
- `frontend/supabase/migrations/009_f03_refund_debt.sql:347-409` — `add_credits` body, no `pg_advisory_xact_lock`.
- `frontend/supabase/migrations/009_f03_refund_debt.sql:101` — `refund_credits` takes `pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0))`.
- `frontend/supabase/migrations/009_f03_refund_debt.sql:231` — `grant_credits` takes the same per-user advisory lock.
- `mariana/api.py:2946-2948, 2999-3001, 3179, 3191, 3210` — `_supabase_add_credits` callers (refund-on-error paths in `start_investigation`).
- `mariana/api.py:6738-` — `_supabase_add_credits` definition (POSTs to `/rest/v1/rpc/add_credits`).
- `mariana/orchestrator/event_loop.py:3397` — `_rpc("add_credits", {…})` from the atomic-probe refund path.

**Reproduction steps (concrete attack chain)**
1. User A has `profiles.tokens = 0` and no open clawbacks.
2. A Stripe `charge.refunded` webhook arrives for an earlier paid grant of 100 credits, so `refund_credits(uid, 100, 'stripe_event', evt_R)` begins. It acquires `pg_advisory_xact_lock(hashtextextended(uid::text, 0))`, debits whatever buckets exist, and (because the buckets are empty in this scenario) inserts a `credit_clawbacks` row of amount 100. It has not yet committed.
3. Concurrently the user starts a new investigation in another tab; the request fails after `spend_credits` reserved 100 credits, so the HTTPException handler at `mariana/api.py:3179` calls `_supabase_add_credits(uid, 100, cfg)`, invoking `add_credits(uid, 100)`.
4. `add_credits` does NOT take the advisory lock (line 347-409). Its first action is `SELECT COALESCE(SUM(amount), 0) INTO v_open_total FROM credit_clawbacks WHERE user_id = uid AND satisfied_at IS NULL`. Because step 2 has not committed yet, `v_open_total = 0`.
5. `add_credits` computes `v_net_addition := GREATEST(0, p_credits - v_open_total) = GREATEST(0, 100 - 0) = 100` and `v_to_net := 100`.
6. `add_credits` enters its `FOR UPDATE` loop on `credit_clawbacks WHERE user_id = uid AND satisfied_at IS NULL`. The row inserted by `refund_credits` is locked by step 2; PostgreSQL waits.
7. Step 2 commits. The lock releases.
8. `add_credits` now sees the freshly visible clawback row (amount 100). Its loop sets `v_satisfy = LEAST(100, 100) = 100`, marks the clawback satisfied, and exits the loop.
9. `add_credits` runs `UPDATE profiles SET tokens = tokens + v_net_addition` with the stale `v_net_addition = 100`. Because step 5 captured 100 instead of 0, the user receives 100 free credits.
10. The clawback was supposed to absorb the full `add_credits` payload; the lock-protected design in `grant_credits` recomputes the net addition under the lock for exactly this reason.

**Impact**
- Silent double-credit: the same 100 credits both satisfy a clawback and land on `profiles.tokens`.
- The mirror invariant (`profiles.tokens` ≈ sum of `credit_buckets.remaining_credits`, modulo open clawbacks) is broken even though every individual SQL statement looks valid.
- The race is reachable from the normal user flow: any HTTPException during `start_investigation` (validation errors, conversation-not-owned, OSError, generic exception) refunds via `add_credits`, and any concurrent refund/dispute webhook for the same user is a candidate for interleaving.
- The orchestrator probe-refund at `event_loop.py:3397` and the agent-route refunds at `mariana/agent/api_routes.py:410, 457` are additional concurrent callers.

**Recommended fix (specific)**
1. Add `PERFORM pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));` as the first statement of `add_credits` (after the `p_credits >= 0` check), matching `grant_credits`/`spend_credits`/`refund_credits`.
2. Recompute `v_open_total` and `v_net_addition` after acquiring the lock so they reflect committed state.
3. Add a regression test that drives `refund_credits` and `add_credits` concurrently for the same user with an empty bucket set and asserts `profiles.tokens` ends at 0 after the clawback is fully satisfied.
4. Backfill any historically over-credited accounts only after the fix lands; identify them by joining `credit_clawbacks` (satisfied) against `credit_transactions` (refund) for matching windows.

**Confidence**: HIGH

---

## I-02 [P2] billing/webhooks | dispute-reversal dedup is TOCTOU, allowing concurrent dispute events to double-debit the same charge

**File(s) + line numbers**
- `mariana/api.py:6324-6389` — `_record_dispute_reversal_or_skip` (SELECT-only check, no insert).
- `mariana/api.py:6392-6443` — `_insert_dispute_reversal` (INSERT with `Prefer: resolution=ignore-duplicates`).
- `mariana/api.py:6446-6568` — `_reverse_credits_for_charge` (calls dedup check, then `_refund_rpc`, then `_insert_dispute_reversal`).
- `mariana/api.py:6530-6537` — `_refund_rpc(ref_id=event_id)` call site.
- `frontend/supabase/migrations/017_h01_h02_stripe_grant_linkage.sql:42-58` — `stripe_dispute_reversals` table (`reversal_key text PRIMARY KEY`).
- `frontend/supabase/migrations/009_f03_refund_debt.sql:103-119` — `refund_credits` idempotency keyed only on `(type='refund', ref_type, ref_id)` and `(credit_clawbacks.ref_type, ref_id)`.

**Reproduction steps (concrete attack chain)**
1. Stripe disputes are filed against a charge that previously granted user U 100 credits via a `stripe_payment_grants` row.
2. Stripe fires `charge.dispute.created` (event ID `evt_A`) and `charge.dispute.funds_withdrawn` (event ID `evt_B`) for the same `dispute.id = dp_X`. Stripe documents that webhook delivery order is not guaranteed and that retries can overlap subsequent events.
3. Both events arrive at the API host pool (multi-replica deployment, or one replica with concurrent requests). Both run `_reverse_credits_for_charge`.
4. Replica 1 executes `_record_dispute_reversal_or_skip`, which issues `GET /rest/v1/stripe_dispute_reversals?reversal_key=eq.dispute:dp_X&select=reversal_key`. The table is empty for this `reversal_key`, so it returns `False`.
5. Replica 2 executes the same SELECT before replica 1 reaches `_insert_dispute_reversal`. It also sees an empty result and returns `False`.
6. Both replicas now call `_refund_rpc(ref_id='evt_A')` and `_refund_rpc(ref_id='evt_B')` respectively. Inside `refund_credits`, the duplicate guard checks `(type='refund', ref_type='stripe_event', ref_id=<event_id>)` — the two `event_id`s are different, so neither call is collapsed. Each call inserts a fresh `credit_transactions` debit and possibly a fresh `credit_clawbacks` deficit row for the same charge.
7. Both replicas then call `_insert_dispute_reversal`. The PRIMARY KEY on `reversal_key` causes one INSERT to be silently dropped by `Prefer: resolution=ignore-duplicates`. The dedup row exists exactly once, but two refunds have already executed.
8. Net result: the user is debited 2× the dispute amount; future grants are absorbed by the over-claimed clawback debt.

**Impact**
- Identical financial impact to H-02, which the migration tried to fix at the dedup-row level. The SELECT-then-INSERT gap leaves a window where two events pass the check.
- The race window is the round-trip latency between the SELECT and the subsequent `_refund_rpc` plus `_insert_dispute_reversal` (typically 50-300 ms over PostgREST). Stripe webhook delivery jitter and retry behavior make this reachable in production, especially with multiple API replicas.
- Single-replica deployments are protected only by the implicit serialization of FastAPI's event loop, which still runs both webhook handlers as concurrent coroutines (each awaits on `httpx`). The race exists even on a single process.

**Recommended fix (specific)**
1. Replace the check-then-insert pattern with a single atomic INSERT-or-skip:
   - Issue `INSERT INTO stripe_dispute_reversals (reversal_key, …) VALUES (…) ON CONFLICT (reversal_key) DO NOTHING RETURNING reversal_key` (PostgREST: `Prefer: return=representation,resolution=ignore-duplicates`) BEFORE calling `_refund_rpc`.
   - If the insert returned no row (because the key already existed), skip the refund.
   - If it returned the row, commit the refund. On `_refund_rpc` failure, DELETE the dedup row (or treat the row as an unsuccessful claim and retry-on-failure under the same key) so the second event can re-attempt.
2. Alternatively, key `refund_credits` idempotency on the same stable `reversal_key` (`dispute:<dispute_id>` or `charge:<charge_id>:reversal`) instead of `event_id`, so the RPC itself collapses both events. Pass `ref_id=reversal_key` from `_reverse_credits_for_charge`.
3. Add a regression test that fires `charge.dispute.created` and `charge.dispute.funds_withdrawn` concurrently (e.g., `asyncio.gather`) for the same dispute and asserts only one `credit_transactions(type='refund')` row is recorded.
4. Audit existing data for two refund rows whose `metadata->>charge_id` (or recovered via `stripe_payment_grants`) matches the same `charge_id` and whose timestamps are within seconds of each other.

**Confidence**: HIGH

---

## I-03 [P3] DB privilege model | `loop6_007_applied` and `loop6_008_applied` are publicly readable and writable via the anon key

**File(s) + line numbers**
- Live database project `afnbtbeayfkwznhzafay`, schema `public`.
- Tables `loop6_007_applied` (columns `applied_at timestamptz`, `one_row boolean`) and `loop6_008_applied` (columns `applied_at timestamptz`, `label text`).
- Likely created by `frontend/supabase/migrations/007_loop6_b02_b05_b06_ledger_sync.sql` and `frontend/supabase/migrations/008_*.sql` (or a similar marker migration); these are the only two `public.*` tables with `pg_class.relrowsecurity = false`.

**Reproduction steps (concrete attack chain)**
1. `SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind='r' AND c.relrowsecurity=false` returns exactly:
   - `loop6_007_applied`
   - `loop6_008_applied`
2. `SELECT grantee, string_agg(privilege_type, ',') FROM information_schema.role_table_grants WHERE table_schema='public' AND table_name IN ('loop6_007_applied','loop6_008_applied') GROUP BY grantee` returns:
   - `anon`: `INSERT, SELECT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER`
   - `authenticated`: `INSERT, SELECT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER`
3. Because RLS is disabled and `anon` has the full DML grant, any browser using only the public anon key can call PostgREST endpoints `GET /rest/v1/loop6_007_applied`, `POST /rest/v1/loop6_007_applied`, `DELETE /rest/v1/loop6_007_applied?...`, etc., with no authentication.
4. Both tables expose only migration metadata, not PII or credits. However, an unauthenticated attacker can:
   - List the rows (information disclosure: confirms which migrations were applied and when).
   - Insert spurious rows with attacker-controlled `applied_at` / `label` values.
   - Truncate or delete existing rows, breaking any future migration runner that uses these markers as preconditions.

**Impact**
- Tampering surface that bypasses the otherwise consistent RLS posture. Every other public table in the live audit had `relrowsecurity = true`.
- Migration framework state can be manipulated. If any guarded re-run logic depends on `EXISTS (SELECT 1 FROM loop6_008_applied)`, an attacker can falsely satisfy or invalidate that precondition.
- Information disclosure of internal migration timestamps and labels.

**Recommended fix (specific)**
1. `ALTER TABLE public.loop6_007_applied ENABLE ROW LEVEL SECURITY;` and `ALTER TABLE public.loop6_008_applied ENABLE ROW LEVEL SECURITY;` with no permissive policies — service_role bypasses RLS, so the migration runner is unaffected.
2. Additionally, `REVOKE ALL ON public.loop6_007_applied FROM anon, authenticated; REVOKE ALL ON public.loop6_008_applied FROM anon, authenticated;` to match the grant posture used by `stripe_payment_grants` / `stripe_dispute_reversals` in migration 017.
3. Verify at the end of the next migration with `pg_class.relrowsecurity` and `information_schema.role_table_grants` and fail if either marker table is browser-reachable.
4. Adopt the convention that any "applied" marker table should be in a private schema (e.g., `migrations.applied`) rather than `public`, so PostgREST does not expose it at all.

**Confidence**: HIGH (verified live)

---

## Thoroughness evidence / areas checked with no new reportable finding

Reviewed files and paths in this audit, in addition to the prior re-audit baselines (A6, A7, A8):

- `loop6_audit/AGENT_BRIEF.md`
- `loop6_audit/REGISTRY.md`
- `loop6_audit/A6_phase_e_reaudit.md`
- `loop6_audit/A7_phase_e_reaudit.md`
- `loop6_audit/A8_phase_e_reaudit.md`
- `loop6_audit/H01_H02_FIX_REPORT.md`
- `mariana/api.py` — webhook dispatch (5670-5710), `_record_dispute_reversal_or_skip` (6324-6389), `_insert_dispute_reversal` (6392-6443), `_reverse_credits_for_charge` (6446-6568), preview/stream HMAC (1622-1807), JWT auth (1216-1494), upload session locking (4636-4678, 2935-3015), `_supabase_add_credits` (6738-).
- `mariana/agent/api_routes.py` — `_supabase_add_credits` callers (`as _refund`).
- `mariana/orchestrator/event_loop.py` — atomic probe refund path (3385-3410).
- `frontend/supabase/migrations/009_f03_refund_debt.sql` — confirmed `pg_advisory_xact_lock` on lines 101 (`refund_credits`) and 231 (`grant_credits`); confirmed it is absent from `add_credits` (lines 347-409).
- `frontend/supabase/migrations/017_h01_h02_stripe_grant_linkage.sql` — `stripe_payment_grants` (PRIMARY KEY `payment_intent_id`), `stripe_dispute_reversals` (PRIMARY KEY `reversal_key`), RLS enabled, anon/authenticated revoked.
- `frontend/supabase/migrations/016_p3_b35_storage_rls.sql`.
- `frontend/src/pages/Chat.tsx:280-367` — `renderMarkdownImpl`. HTML-escapes `& < >` first; `<pre>` blocks are tokenized; markdown link `href` is restricted to `^https?://` and escapes `" ' `` ` in both URL and link text.
- `frontend/src/components/FileViewer.tsx:80-160` — `renderMarkdownContent`. HTML-escapes `& < > " '` first; markdown link `href` is decoded then re-validated against `^https?://`; non-http(s) links are emitted as plain text.
- `frontend/src/components/ui/chart.tsx:61-88` — `<style dangerouslySetInnerHTML={{ __html: … }} />` injects CSS variables from a `ChartConfig` object. The `chart.tsx` module is currently not used (no imports found in `frontend/src/`), so there is no live exploit surface even though a CSS-injection vector exists in principle if user-controlled `color` values were ever passed in.
- `frontend/vercel.json` — `Content-Security-Policy: default-src 'self'; … script-src 'self' 'wasm-unsafe-eval' https://js.stripe.com; …`. No `unsafe-inline`/`unsafe-eval` for scripts; HSTS, X-Frame-Options DENY, Permissions-Policy, COOP/CORP all present.
- Live Supabase project `afnbtbeayfkwznhzafay`:
  - All public tables except `loop6_007_applied` and `loop6_008_applied` have RLS enabled (see I-03).
  - `stripe_payment_grants`, `stripe_dispute_reversals`: RLS enabled, anon/authenticated have no privileges (only postgres + service_role).
  - All `SECURITY DEFINER` functions in `public` (`add_credits`, `refund_credits`, `grant_credits`, `spend_credits`, `deduct_credits`, `expire_credits`, `admin_*`, `check_balance`, `handle_new_user`, etc.) have explicit `proconfig` `search_path` set (`public, pg_temp` or `public, auth`) — no missing search-path hardening on functions added in migrations 010-017.
  - Tables `research_tasks`, `hypotheses`, `claims`, `perspective_syntheses`, `intelligence_jobs`, `task_perspectives` do not exist in production — A8's caveat about backend-created table RLS is moot.

Non-findings after re-check:
- Preview/stream token verification continues to use `hmac.compare_digest`, includes a fixed `preview` scope marker, and tolerates only 5s clock skew. No new reportable issue.
- F-06 cursor format is unsigned but task-scoped (`WHERE c.task_id = $1`) and ownership is enforced at the route via `_require_investigation_owner`. No new privilege-escalation path identified.
- Upload-session strong-ref OrderedDict with held-lock-skip eviction (G-01 fix) remains sound under single-process asyncio.
- Stripe webhook event coverage is intentional: unhandled event types fall to the `else` branch and return 200, which is correct (Stripe retries non-2xx). `invoice.payment_failed`, `customer.subscription.trial_will_end`, and `customer.subscription.paused` are not financial events that change credit grants.
- F-04 plan entitlement (`_effective_plan`) maps subscription state to plan correctly; the `flagship`/`max` naming inconsistency persists but no clean attacker-value bypass identified within the stated adversary model.
- Vault KDF defaults (`m=64MiB, t=3, p=4`) are OWASP-compliant. The configurable minimum (`m=16MiB`) is below the OWASP `m=19MiB` recommendation but is enforced server-side and not user-tunable from the browser; not a reportable finding.
- `chart.tsx` `dangerouslySetInnerHTML` is a CSS-only sink, populated from a `ChartConfig` that is not currently referenced anywhere in `frontend/src/`. No live exploit path.
- The `property-images` storage bucket finding from earlier candidate notes was not promoted: it has 0 objects, no app references it, and migration 016 added authenticated RLS policies. While the bucket is `public=true`, no user content can land there through any code path in the current repo, so there is no exploitable vector at this revision.
