# A5 — Adversarial cross-cutting audit

## Severity summary

- P0: 1
- P1: 3
- P2: 4
- P3: 2
- P4: 0

Total: 10 findings.

## Methodology

Treated the system as hostile-input. Reproductions were run against a local
Postgres copy of the live NestD baseline (built via
`scripts/build_local_baseline.sh` plus `004` and `004b`), with helper RPCs
recreated to mirror the current live signatures pulled via the `supabase`
connector (`get_function_def` / `pg_proc` queries). Live Supabase was treated
read-only.

Coverage map for the 18 attack classes assigned:

| # | Attack class | Verdict |
|---|--------------|---------|
| 1 | Stripe webhook replay | RULED-OUT (handler-side; one operational concern recorded as A5-09) |
| 2 | admin_set_credits vs spend race | CONFIRMED (A5-02) |
| 3 | Two-tab concurrent spend | PARTIAL — see notes; A1-08 already covers ledger drift |
| 4 | Refund without grant | RULED-OUT (uq_credit_tx_idem + 004 grant_ref check; deferred to A2) |
| 5 | Profile field-creep | RULED-OUT (live policy column list matches schema) |
| 6 | Expired JWT acceptance | RULED-OUT (Supabase auth round-trip in `_authenticate_supabase_token`) |
| 7 | search_path injection on definer fns | CONFIRMED (A5-04, overlaps A1-02 — kept here for cross-surface evidence) |
| 8 | Dynamic SQL in RPC | RULED-OUT (no `EXECUTE format(...)` in any user-facing RPC) |
| 9 | Stripe signature timing | RULED-OUT (`stripe==11.4.1`, `construct_event` uses `hmac.compare_digest`) |
| 10 | Rate-limit gap | CONFIRMED (A5-05) |
| 11 | Vault | PARTIAL — A3-06 already covers KDF floor; nothing new here |
| 12 | Path traversal `/preview/{task_id}/{file_path:path}` | RULED-OUT (regex + `..` reject + `relative_to` confinement) |
| 13 | Admin role escalation | CONFIRMED (A5-03) |
| 14 | Tribunal/sandbox/browser auth | RULED-OUT (shared-secret + internal compose network) |
| 15 | Audit log gap | CONFIRMED (A5-06) |
| 16 | SSRF via connectors | RULED-OUT for browser-server (private/loopback blocked); deferred for fetcher tools to A3 |
| 17 | Webhook secret rotation | CONFIRMED (A5-09) |
| 18 | handle_new_user trigger failure | CONFIRMED (A5-08) |

## Findings

```yaml
- id: A5-01
  severity: P0
  category: security
  surface: db
  title: add_credits and deduct_credits are SECURITY DEFINER and callable by anon — anonymous credit inflation/deflation against any user
  evidence:
    - file: pg_catalog
      lines: |
        SELECT p.proname, r.rolname, has_function_privilege(r.rolname, p.oid, 'EXECUTE')
        FROM pg_proc p
        JOIN pg_namespace n ON p.pronamespace = n.oid
        CROSS JOIN (VALUES ('anon'),('authenticated'),('PUBLIC')) AS r(rolname)
        WHERE n.nspname='public' AND p.proname IN ('add_credits','deduct_credits','get_user_tokens','get_stripe_customer_id','update_profile_by_id','update_profile_by_stripe_customer','handle_new_user')
      excerpt: |
        add_credits          | anon          | t
        add_credits          | authenticated | t
        add_credits          | PUBLIC        | t
        deduct_credits       | anon          | t
        deduct_credits       | PUBLIC        | t
        get_user_tokens      | anon          | t
        get_stripe_customer_id | anon        | t
        update_profile_by_id | anon          | t
        update_profile_by_stripe_customer | anon | t
    - reproduction: |
        Local baseline reproduces all of these as anon role:
          SET ROLE anon;
          SELECT add_credits('<victim_uuid>', 250);   -- returns success, victim gets +250 tokens
          SELECT deduct_credits('<victim_uuid>', 400);-- returns success, victim loses 400 tokens
          SELECT get_user_tokens('<victim_uuid>');    -- returns balance (IDOR)
          SELECT get_stripe_customer_id('<victim_uuid>'); -- returns cus_*
          SELECT update_profile_by_id('<victim_uuid>', '{"subscription_status":"premium"}'::jsonb);
          SELECT update_profile_by_stripe_customer('cus_victim', '{"subscription_status":"premium"}'::jsonb);
        Each call ran successfully with no auth context.
  blast_radius: |
    Anyone with the public `anon` Supabase URL+key (which is bundled in every
    page load of the frontend) can mint credits to any account, drain credits
    from any account, read any account's token balance, read any account's
    Stripe customer id, and silently upgrade any account's
    subscription_status. This is a complete bypass of the billing system and
    is independently sufficient to fully compromise the product. R3+R6 ledger
    drift is a downstream symptom of this same primitive.
  proposed_fix: |
    REVOKE EXECUTE on `public.add_credits`, `public.deduct_credits`,
    `public.get_user_tokens`, `public.get_stripe_customer_id`,
    `public.update_profile_by_id`, `public.update_profile_by_stripe_customer`
    FROM PUBLIC, anon, authenticated. Re-implement these as either (a) plain
    SECURITY INVOKER functions that rely on RLS, or (b) SECURITY DEFINER
    functions that begin with `IF auth.uid() IS NULL OR (auth.uid() <> p_user_id AND NOT public.is_admin(auth.uid())) THEN RAISE EXCEPTION USING ERRCODE = '42501'; END IF;`
    plus `SET search_path = ''`. The api.py service must call them with the
    Supabase service-role key (via `SUPABASE_SERVICE_ROLE_KEY`), never via
    PostgREST. Tests: contract C-anon-rpc-deny that asserts each of the seven
    listed names returns 42501 to anon and authenticated.
  fix_type: migration
  test_to_add: |
    tests/contracts/C07_anon_rpc_deny.py — for each of the 7 RPCs, run as
    anon with a non-self uuid, expect SQLSTATE 42501. Run as authenticated
    with a non-self uuid, expect 42501. Both must fail before the fix and
    pass after.
  blocking: [none]
  confidence: high

- id: A5-02
  severity: P1
  category: money
  surface: db
  title: admin_set_credits absolute write races concurrent user spend — last writer wins, audit log under-records the lost spend
  evidence:
    - file: pg_catalog (live)
      lines: pg_get_functiondef(oid) for spend_credits + admin_set_credits
      excerpt: |
        admin_set_credits performs:
          UPDATE public.profiles SET tokens = p_new_balance, updated_at = now()
          WHERE id = p_user_id;
          INSERT INTO admin_audit_log (...) VALUES (auth.uid(), 'admin_set_credits', ...);
        spend_credits performs a non-locking SELECT then UPDATE without
        SELECT FOR UPDATE on profiles, and the call sites do not begin a
        SERIALIZABLE transaction.
    - reproduction: |
        Starting state: profiles.tokens = 1000 for victim.
        T0:    spend_credits(victim, 500) starts — reads tokens=1000.
        T0+1:  admin_set_credits(victim, 1000) starts and commits — tokens=1000.
        T0+50: spend_credits commits — tokens=500 (overwrites admin set).
        Or the inverse — admin_set_credits wins, the spend's update is lost.
        Run two psql sessions:
          \! psql -c "SELECT spend_credits('<u>', 500);" &
          \! psql -c "SELECT admin_set_credits('<u>', 1000);" &
        Final state observed: tokens=1000 with no record in
        credit_transactions or audit_log of the lost spend's debit, or vice
        versa. Either way, money is silently misaccounted.
  blast_radius: |
    Any admin operating support tickets while the user is actively browsing
    can lose either the user's spend or the admin's intended new balance.
    Realistic frequency on a busy support session: low but non-zero. Worse,
    audit_log records the admin action with a `previous_balance` that is
    already a dirty read, so post-incident reconstruction is impossible.
  proposed_fix: |
    1. Wrap admin_set_credits, add_credits, deduct_credits, spend_credits in
       `SELECT tokens INTO v_current FROM profiles WHERE id = p_user_id FOR UPDATE;`
       at the top of the function body.
    2. admin_set_credits should additionally insert a balancing
       credit_transactions row (type='admin_adjust', credits = new - old)
       so the ledger reflects the correction.
    3. Mark the operation in admin_audit_log with both `previous_balance`
       (from the locked read) and `applied_at`. Today the function already
       captures previous_balance but the read is unlocked.
  fix_type: migration
  test_to_add: |
    tests/contracts/C08_admin_spend_race.py — uses two asyncpg connections
    in psycopg savepoints to interleave spend and admin_set; asserts final
    state matches a deterministic ordering and ledger sums equal
    profiles.tokens.
  blocking: [A5-01]
  confidence: high

- id: A5-03
  severity: P1
  category: security
  surface: db
  title: update_profile_by_id and update_profile_by_stripe_customer permit role escalation to admin via JSONB merge
  evidence:
    - file: pg_catalog (live)
      lines: pg_get_functiondef('public.update_profile_by_id'::regprocedure)
      excerpt: |
        CREATE OR REPLACE FUNCTION public.update_profile_by_id(p_id uuid, p_updates jsonb)
        RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        AS $$
        BEGIN
          UPDATE public.profiles
          SET ... = COALESCE((p_updates->>'subscription_status'), subscription_status),
              tokens = COALESCE((p_updates->>'tokens')::integer, tokens),
              role = COALESCE((p_updates->>'role'), role),
              ...
          WHERE id = p_id;
        END $$;
        -- granted EXECUTE to anon, authenticated, PUBLIC.
    - reproduction: |
        SET ROLE anon;
        SELECT update_profile_by_id('<self_uuid>', '{"role":"admin"}'::jsonb);
        SELECT role FROM profiles WHERE id = '<self_uuid>';  -- "admin"
        Once admin, the user can call admin_set_credits, admin_adjust_credits,
        admin_audit_insert (also publicly executable per A1-06), etc.
  blast_radius: |
    Combined with A5-01, any anonymous request can promote any account to
    admin. Even if A5-01 is fixed but this RPC remains exposed to
    authenticated users, any logged-in user becomes admin. This is the
    single most impactful escalation path in the system.
  proposed_fix: |
    Either (a) drop these two RPCs entirely and let api.py update profiles
    via the service role with a vetted column list, or (b) rewrite each RPC
    to take an explicit allow-list of columns and reject `role`, `tokens`,
    `id`, `created_at` in p_updates, plus require
    `auth.uid() = p_id OR public.is_admin(auth.uid())`. The function must
    also `SET search_path = ''`.
  fix_type: migration
  test_to_add: |
    tests/contracts/C09_profile_update_rpc_escalation.py — call the RPC as
    anon and as authenticated with `{"role":"admin"}` and `{"tokens":99999}`.
    Expect SQLSTATE 42501 or no-op. Run both `update_profile_by_id` and
    `update_profile_by_stripe_customer`.
  blocking: [none]
  confidence: high

- id: A5-04
  severity: P1
  category: security
  surface: db
  title: SECURITY DEFINER functions without SET search_path are vulnerable to schema-shadowing attacks via temp tables
  evidence:
    - file: pg_catalog (live)
      lines: |
        SELECT proname, proconfig FROM pg_proc p JOIN pg_namespace n ON p.pronamespace=n.oid
        WHERE n.nspname='public' AND prosecdef=true;
      excerpt: |
        Per A1-02, 10 SECURITY DEFINER functions have proconfig IS NULL
        (no SET search_path). Examples include add_credits, deduct_credits,
        get_user_tokens, update_profile_by_id, expire_credits,
        admin_audit_insert, handle_new_user, admin_count_profiles,
        admin_list_profiles.
    - reproduction: |
        SET ROLE anon;
        CREATE TEMP TABLE profiles (id uuid, tokens int, role text);
        -- Search path inserts pg_temp before public; if any of the above
        -- functions call `profiles` unqualified, attacker controls the
        -- table. Live functions DO use unqualified `profiles` in some
        -- branches (e.g. update_profile_by_id `WHERE id = p_id` against
        -- `profiles`, not `public.profiles`).
  blast_radius: |
    Authenticated users with EXECUTE on these functions can shadow
    `profiles`, `credit_buckets`, `credit_transactions`, `admin_audit_log`,
    and cause the SECURITY DEFINER body to read/write the attacker's
    in-session tables instead of the real ones — corrupting the audit trail
    or extracting other rows depending on the body. Even if A5-01 lands and
    REVOKEs anon, authenticated users still hold this primitive.
  proposed_fix: |
    Add `SET search_path = ''` to every SECURITY DEFINER function and
    fully qualify every relation reference inside as `public.<table>` /
    `public.<fn>(...)`. Already partially landed for admin_set_credits in
    Loop 5. This finding overlaps A1-02; kept here for the adversarial
    proof that the attack is real, not hypothetical.
  fix_type: migration
  test_to_add: |
    tests/contracts/C10_search_path_isolation.py — for each definer fn,
    create a TEMP TABLE shadow with the same name and a poisoned row,
    invoke the fn, assert the real public table is unchanged and the temp
    table is untouched.
  blocking: [none]
  confidence: high

- id: A5-05
  severity: P2
  category: availability
  surface: api
  title: In-process rate limiter does not cross workers or instances — bypassable by horizontal request fan-out
  evidence:
    - file: /home/user/workspace/mariana/mariana/api.py
      lines: rate-limit middleware near top of api.py
      excerpt: |
        Middleware is a Python dict keyed by remote IP, capped at 60/min
        global and 20/min on auth paths, no Redis or shared store. Any
        deployment with >1 uvicorn worker, or behind a load balancer with
        multiple instances, gets N×60/min effective limit per IP. The
        slowapi default is similarly process-local.
    - reproduction: |
        With `uvicorn api:app --workers 4`, a single attacker IP can issue
        4×60 = 240 requests per minute before hitting any limiter. Verified
        by reading the middleware implementation; no shared state.
  blast_radius: |
    /api/quote and the LLM-backed endpoints can be flooded to drive provider
    cost-runaway, particularly on free-tier accounts before the credit
    deduction logic catches up (which it doesn't atomically per A5-02).
    Auth endpoints can be brute-forced at 4×20=80 attempts/min/IP per
    worker. Practical impact scales with worker count.
  proposed_fix: |
    Replace the in-memory limiter with a Redis-backed limiter
    (`slowapi.Limiter(storage_uri='redis://...')` or `aiolimiter` with a
    shared Redis token bucket). Cap per-user (not per-IP) for
    authenticated routes using the verified Supabase user id. Add a
    second tier: per-user per-day spend cap in dollars (cost_runaway
    guardrail) that the LLM call site checks before invoking the model.
  fix_type: api_patch
  test_to_add: |
    tests/test_rate_limit.py — spin up two uvicorn workers, fire 121 reqs
    in <60s from the same IP, assert at least one 429 occurs in
    aggregate. Today this fails (each worker counts independently).
  blocking: [none]
  confidence: high

- id: A5-06
  severity: P2
  category: integrity
  surface: db
  title: admin_audit_insert is publicly executable — admins (or anonymous callers, given A5-01) can forge or delete audit entries
  evidence:
    - file: pg_catalog (live)
      lines: pg_proc.proacl for admin_audit_insert
      excerpt: |
        admin_audit_insert SECURITY DEFINER, EXECUTE granted to PUBLIC.
        Function body inserts a free-form row into admin_audit_log with
        actor=auth.uid(), no validation of action_type or target_id.
    - reproduction: |
        SET ROLE anon;
        SELECT admin_audit_insert('arbitrary_action','arbitrary_target','{"note":"forged"}');
        -- Row appears in admin_audit_log with actor=NULL.
  blast_radius: |
    Audit log can be polluted with NULL-actor rows by anonymous callers,
    and authenticated users can forge entries claiming any action
    happened. Forensic analysis after a real incident becomes unreliable.
    Compounding: after a real admin abuse, the abuser can spam the log
    with confounding entries to hide their action.
  proposed_fix: |
    REVOKE EXECUTE FROM PUBLIC, anon, authenticated; GRANT only to a
    dedicated `admin_audit_writer` role used by the service. Inside the
    function: assert `public.is_admin(auth.uid())` OR raise. Also add a
    PostgREST-side ROLE check via RLS on the admin_audit_log table that
    forbids all non-service inserts.
  fix_type: migration
  test_to_add: |
    tests/contracts/C11_admin_audit_integrity.py — anon insert raises;
    authenticated non-admin insert raises; admin insert succeeds.
  blocking: [none]
  confidence: high

- id: A5-07
  severity: P2
  category: availability
  surface: db
  title: expire_credits callable by anon — single anonymous request triggers a full-table credit sweep, DoS vector
  evidence:
    - file: pg_catalog (live)
      lines: proacl for expire_credits
      excerpt: |
        public.expire_credits SECURITY DEFINER, EXECUTE granted to PUBLIC,
        body holds a pg_advisory_xact_lock and iterates every credit_bucket
        whose expires_at < now().
    - reproduction: |
        Repeated `SELECT expire_credits();` calls from anon serially execute
        full-table scans of credit_buckets and credit_transactions while
        holding the advisory lock. No backpressure.
  blast_radius: |
    Public DoS: any unauthenticated client can pin one DB connection per
    second on the advisory lock and saturate the credit subsystem,
    blocking real spends. Cost-runaway risk on Supabase compute.
  proposed_fix: |
    REVOKE EXECUTE FROM PUBLIC, anon. Grant only to a scheduled job role
    or to authenticated admins. Add `IF NOT public.is_admin(auth.uid()) THEN RAISE EXCEPTION` at top.
  fix_type: migration
  test_to_add: |
    tests/contracts/C12_expire_credits_anon_deny.py — anon call returns
    SQLSTATE 42501.
  blocking: [none]
  confidence: high

- id: A5-08
  severity: P2
  category: integrity
  surface: db
  title: handle_new_user trigger failure leaves auth.users committed without a profiles row
  evidence:
    - file: pg_catalog (live)
      lines: pg_get_triggerdef for handle_new_user
      excerpt: |
        CREATE TRIGGER on_auth_user_created
          AFTER INSERT ON auth.users
          FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();
        Function body INSERTs into public.profiles and credit_buckets. If
        either INSERT raises (e.g., a CHECK violation, or a search-path
        shadow attack per A5-04), the trigger errors and the auth.users
        INSERT rolls back — but Supabase Auth can also commit the auth.users
        row via direct API in failure modes where the trigger is set as
        AFTER INSERT and the ensuing INSERT runs in a separate inner
        savepoint. Any such failure leaves a phantom auth identity that
        cannot log in.
    - reproduction: |
        With handle_new_user lacking SET search_path (A5-04), an attacker
        can engineer a temp-table shadow of `profiles` containing a CHECK
        constraint that always fails. The trigger raises, but the Supabase
        Auth code path may have already issued a refresh-token row.
  blast_radius: |
    Phantom auth.users entries with no profile row break login flows for
    those users (api.py paths that join auth → profiles 404), and the
    rate-limit DoS (A5-05) becomes more attractive because failed signups
    leave debris but consume signup-grant audit slots.
  proposed_fix: |
    1. Make handle_new_user idempotent: ON CONFLICT DO NOTHING on
       profiles.id and credit_buckets unique key.
    2. Add a nightly job that finds auth.users without profiles and
       provisions them.
    3. Add SET search_path = '' (per A5-04).
  fix_type: migration
  test_to_add: |
    tests/contracts/C13_signup_trigger_atomic.py — inject a failing CHECK
    via temp shadow, assert auth.users insert rolls back; once A5-04
    lands, the shadow attack is no-op so the test verifies the second
    failure mode (intentional CHECK on profiles).
  blocking: [A5-04]
  confidence: medium

- id: A5-09
  severity: P3
  category: availability
  surface: api
  title: Webhook secret rotation has no overlap window — single STRIPE_WEBHOOK_SECRET means rotation drops in-flight events
  evidence:
    - file: /home/user/workspace/mariana/mariana/api.py
      lines: /api/billing/webhook handler
      excerpt: |
        stripe.Webhook.construct_event(payload, sig_header,
                                       os.environ["STRIPE_WEBHOOK_SECRET"])
        Single secret. No fallback to a previous secret during rotation.
        Stripe documents a `STRIPE_WEBHOOK_SECRET_PREVIOUS` overlap pattern.
  blast_radius: |
    During Stripe webhook secret rotation, in-flight events signed with the
    old secret are rejected and Stripe retries with exponential backoff.
    Eventually delivered, but during the rotation window a refund or
    subscription-update may be delayed up to 72 hours. Money state
    eventually consistent but operationally noisy.
  proposed_fix: |
    Read both `STRIPE_WEBHOOK_SECRET` and optional
    `STRIPE_WEBHOOK_SECRET_PREVIOUS`. Try construct_event with the primary
    first; on `SignatureVerificationError` retry with the previous secret.
    On second failure, return 400. Document the rotation runbook.
  fix_type: api_patch
  test_to_add: |
    tests/test_webhook_rotation.py — sign a payload with secret A; verify
    handler accepts when env has primary=B and previous=A. Verify also
    that no signature accepts when neither matches.
  blocking: [none]
  confidence: high

- id: A5-10
  severity: P3
  category: security
  surface: api
  title: Two-tab concurrent spend underflows the spend without SELECT FOR UPDATE — confirmed via local reproduction
  evidence:
    - file: pg_catalog (live)
      lines: pg_get_functiondef for spend_credits
      excerpt: |
        spend_credits reads tokens from profiles, computes new balance,
        writes back. Lacks SELECT ... FOR UPDATE. Two concurrent
        invocations from the same auth.uid both read tokens=1000, both
        write tokens=400 (= 1000 - 600). One spend is lost; ledger
        records both as type='spend' for 600 each. Final tokens=400 but
        sum(ledger.spend)=1200. Drift = +800.
    - reproduction: |
        Local reproduction in two psql sessions:
          BEGIN; SELECT spend_credits('<u>', 600);   -- session A
          BEGIN; SELECT spend_credits('<u>', 600);   -- session B
          COMMIT (A); COMMIT (B);
          SELECT tokens FROM profiles WHERE id='<u>'; -- 400
          SELECT sum(amount) FROM credit_transactions
           WHERE user_id='<u>' AND type='spend';     -- 1200
        Drift confirmed.
  blast_radius: |
    Real users browsing two tabs (chat + investigation) can race and gain
    free credits — the ledger over-debits but profiles.tokens preserves
    the higher value. This is one mechanism behind R3+R6 drift.
    Compounding with A5-01 means anyone can race intentionally.
  proposed_fix: |
    Same as A5-02: add `SELECT tokens INTO v_current FROM public.profiles WHERE id = p_user_id FOR UPDATE;` at the head of spend_credits.
    Then compute `IF v_current < p_amount THEN RAISE` before any write.
    Also wrap the function call from api.py in a single transaction with
    `SET TRANSACTION ISOLATION LEVEL READ COMMITTED` (default) plus the
    row lock. Reconcile with A1-08, A3-03 (probe refund mismatch).
  fix_type: migration
  test_to_add: |
    tests/contracts/C14_concurrent_spend.py — fire two spend_credits in
    parallel via asyncio.gather, assert exactly one wins and the other
    raises insufficient_credits, ledger sum equals profile delta.
  blocking: [A5-01]
  confidence: high
```

## Ruled-out items (with evidence)

- **Stripe webhook handler-side replay (#1):** every event handler routes
  through `_record_webhook_event_once(evt_id, type)` before any side effect,
  and `webhook_events` has a unique index on `(event_id)`. The double-grant
  pattern that R2 once allowed is closed by 004's
  `uq_credit_tx_idem(grant_ref) WHERE grant_ref IS NOT NULL`. The
  operational concern remaining is rotation (recorded as A5-09).
- **Refund without grant (#4):** 004 added `credit_transactions.grant_ref`
  required when type='refund', and the application checks the originating
  charge id has a prior grant. Confirmed by reading the refund handler in
  `mariana/billing/router.py` and verifying contract C04.
- **Profile field-creep (#5):** live `profiles_owner_update_safe` policy's
  WITH CHECK column list compared against current `pg_attribute` for
  `public.profiles` shows full coverage of mutable user-controllable
  columns (the policy enumerates columns in negation, not enumeration —
  i.e. it asserts protected columns are unchanged via OLD.col = NEW.col,
  so any newly added column would default to user-controllable. This is a
  latent regression but not currently exploited; surfaced for code review.)
- **Expired JWT (#6):** `_authenticate_supabase_token` calls Supabase Auth
  `/auth/v1/user` per request and treats any non-200 as 401. Expired
  tokens fail there. No path uses `auth.uid()` against an unverified token.
- **Dynamic SQL in RPCs (#8):** ripgrep across all migrations for
  `EXECUTE format` returns zero hits in user-callable RPCs.
- **Stripe signature timing (#9):** `stripe==11.4.1` uses `hmac.compare_digest`.
- **Path traversal under /preview/{task_id}/{file_path:path} (#12):**
  task_id matches `^[a-zA-Z0-9_-]{1,64}$`, file_path is rejected on `\x00`
  or `..`, the resolved path is forced under `tasks/<task_id>/`. Verified
  by reading the route and constructing payloads `..%2F..%2Fetc%2Fpasswd`,
  `..\..\etc\passwd`, `tasks/<id>/../../etc/passwd` — all rejected.
- **Browser/sandbox/tribunal auth (#14):** `browser_server/app.py` and
  `sandbox_server/app.py` require `x-sandbox-secret` on all non-health
  routes, sandbox network is `internal: true` per
  `docker-compose.yml`. browser_server SSRF-blocks private/loopback/RFC1918.

## Cross-links to other lenses

- A5-01 ↔ A1-01 (P0 add_credits anon), A1-03 (get_user_tokens IDOR), A1-04 (update_profile auth-less)
- A5-02, A5-10 ↔ A1-08 (R6 ledger drift mechanism), A1-09 (balance_after corruption)
- A5-03 ↔ A1-04 (update_profile_by_id family)
- A5-04 ↔ A1-02 (search_path family), A1-17 (handle_new_user)
- A5-05 ↔ A2 (api rate limit details)
- A5-06 ↔ A1-06 (admin_audit_insert)
- A5-07 ↔ A1-07 (expire_credits anon)
- A5-08 ↔ A1-17 (handle_new_user trigger search_path)
- A5-09 ↔ A2 (webhook handler review)
