# A1 — Live DB Audit Findings
## NestD project `afnbtbeayfkwznhzafay` — Loop 6

**Severity summary**
| P0 | P1 | P2 | P3 | P4 |
|----|----|----|----|----|
| 1  | 3  | 5  | 5  | 0  |

---

## CATEGORY: security

```yaml
- id: A1-01
  severity: P0
  category: security
  surface: db
  title: add_credits is SECURITY DEFINER with no caller-identity check, callable by anon, no search_path
  evidence:
    - file: pg_catalog / information_schema.role_routine_grants
      lines: "proname=add_credits, prosecdef=true, proconfig=null, ACL includes =X/postgres (PUBLIC) and anon=X/postgres"
      excerpt: |
        CREATE OR REPLACE FUNCTION public.add_credits(p_user_id uuid, p_credits integer)
         RETURNS void
         LANGUAGE plpgsql
         SECURITY DEFINER   -- no SET search_path
        AS $function$
        BEGIN
          IF p_credits < 0 THEN
            RAISE EXCEPTION 'Credits amount must be non-negative, got %', p_credits;
          END IF;
          UPDATE profiles
          SET tokens = tokens + p_credits,
              updated_at = now()
          WHERE id = p_user_id;
        ...
        -- ACL: =X/postgres (PUBLIC EXECUTE), anon=X/postgres, authenticated=X/postgres
    - reproduction: |
        Any unauthenticated client can POST to:
          POST https://afnbtbeayfkwznhzafay.supabase.co/rest/v1/rpc/add_credits
          Content-Type: application/json
          apikey: <anon_key>
          {"p_user_id": "<any_valid_uuid>", "p_credits": 99999999}
        This will add credits to any user's profile.tokens without any authentication,
        admin check, or audit log entry. The function also modifies profiles.tokens
        directly, bypassing the credit_buckets/credit_transactions ledger entirely
        (a separate R6 issue).
  blast_radius: |
    Any unauthenticated actor (or authenticated non-admin user) can increment
    any user's token balance to an arbitrary amount by calling /rpc/add_credits
    with a target UUID. No auth check, no ownership check, no audit trail.
    The function also has no SET search_path, meaning a rogue schema object
    in a session-controlled search_path could intercept the profiles table
    reference. With 13 live users and real token balances, this is an active
    financial-integrity and auth bypass P0.
  proposed_fix: |
    Either (a) REVOKE ALL ON FUNCTION public.add_credits FROM PUBLIC, anon, authenticated
    and keep it callable only by service_role (the pattern used for grant_credits,
    spend_credits, refund_credits, expire_credits which correctly omit PUBLIC/anon
    from their ACL), OR (b) add a caller-identity guard (is_admin or ownership check)
    AND add SET search_path = 'public', AND add an audit log entry. Option (a) is
    strongly preferred. Also add ledger writes (credit_buckets/credit_transactions)
    for R6 consistency.
  fix_type: migration
  test_to_add: |
    test_add_credits_anon_blocked: call /rpc/add_credits with anon JWT and any
    target UUID, expect HTTP 403. Also verify no row was modified.
    test_add_credits_no_search_path: verify proconfig on add_credits includes
    search_path after fix.
  blocking: [none]
  confidence: high
```

```yaml
- id: A1-02
  severity: P1
  category: security
  surface: db
  title: 10 SECURITY DEFINER functions lack SET search_path — search-path injection vector
  evidence:
    - file: pg_catalog (get_advisors security + direct proconfig inspection)
      lines: "proconfig IS NULL for: add_credits, admin_count_profiles, admin_list_profiles, check_balance, deduct_credits, get_stripe_customer_id, get_user_tokens, handle_new_user, update_profile_by_id, update_profile_by_stripe_customer"
      excerpt: |
        -- Advisor finding (function_search_path_mutable, level=WARN):
        "Function `public.handle_new_user` has a role mutable search_path"
        "Function `public.admin_list_profiles` has a role mutable search_path"
        "Function `public.admin_count_profiles` has a role mutable search_path"
        "Function `public.deduct_credits` has a role mutable search_path"
        "Function `public.check_balance` has a role mutable search_path"
        "Function `public.add_credits` has a role mutable search_path"
        "Function `public.get_user_tokens` has a role mutable search_path"
        "Function `public.get_stripe_customer_id` has a role mutable search_path"
        "Function `public.update_profile_by_id` has a role mutable search_path"
        "Function `public.update_profile_by_stripe_customer` has a role mutable search_path"

        -- Contrast: admin_set_credits DOES have search_path='' (fixed in 004)
        -- but the remaining 10 SECURITY DEFINER functions do not.
    - reproduction: |
        If a session can set search_path (e.g. via a rogue schema injection in
        a SECURITY INVOKER outer function), calling any of these functions
        while the session search_path resolves a shadow 'profiles' table or
        'auth' schema first will make the SECURITY DEFINER function operate on
        the wrong objects.
  blast_radius: |
    All 10 functions operate with postgres-owner privileges on user data: tokens,
    stripe customer IDs, subscriptions. Without a pinned search_path, a
    search_path attack (e.g. via CREATE SCHEMA evil; CREATE TABLE evil.profiles ...)
    could redirect these functions to attacker-controlled tables. deduct_credits
    and update_profile_by_id/by_stripe_customer are particularly sensitive:
    deduct_credits handles billing; update_profile_by_id and update_profile_by_stripe_customer
    handle Stripe webhook data including subscription_status. R5 was fixed for
    admin_set_credits only; these 10 were missed. The prior 16-audit PASS record
    missed this entire class on all but one function.
  proposed_fix: |
    Add SET search_path = 'public', 'pg_temp' (or '' with fully-qualified refs)
    to all 10 functions. Batch migration:
      CREATE OR REPLACE FUNCTION public.deduct_credits(...) ... SET search_path = 'public', 'pg_temp' ...
    Follow the admin_set_credits precedent from migration 004. Functions that
    reference auth.uid() need 'auth' in search_path too (e.g. handle_new_user).
  fix_type: migration
  test_to_add: |
    test_all_secdef_have_search_path: SELECT proname FROM pg_proc p JOIN pg_namespace n
    ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.prosecdef = true
    AND (p.proconfig IS NULL OR NOT EXISTS (SELECT 1 FROM unnest(p.proconfig) e
    WHERE e ILIKE 'search_path=%')) — must return 0 rows after fix.
  blocking: [A1-01]
  confidence: high
```

```yaml
- id: A1-03
  severity: P1
  category: security
  surface: db
  title: check_balance and get_user_tokens expose any user's token balance to any caller — IDOR
  evidence:
    - file: pg_catalog (prosrc + ACL)
      lines: "check_balance, get_user_tokens: SECURITY DEFINER, ACL includes PUBLIC + anon"
      excerpt: |
        -- check_balance:
        CREATE OR REPLACE FUNCTION public.check_balance(target_user_id uuid)
         RETURNS integer  LANGUAGE sql  SECURITY DEFINER
        AS $function$
          SELECT tokens FROM profiles WHERE id = target_user_id;
        $function$
        -- ACL: =X/postgres (PUBLIC), anon=X/postgres, authenticated=X/postgres

        -- get_user_tokens:
        CREATE OR REPLACE FUNCTION public.get_user_tokens(target_user_id uuid)
         RETURNS integer  LANGUAGE plpgsql  SECURITY DEFINER
        AS $function$
        BEGIN
          SELECT tokens INTO result FROM profiles WHERE id = target_user_id;
          RETURN COALESCE(result, 0);
        END;
        $function$
        -- ACL: =X/postgres (PUBLIC), anon=X/postgres, authenticated=X/postgres
    - reproduction: |
        GET https://afnbtbeayfkwznhzafay.supabase.co/rest/v1/rpc/check_balance
        or /rpc/get_user_tokens
        apikey: <anon_key>
        body: {"target_user_id": "<any_valid_user_uuid>"}
        Returns that user's token balance without any auth.
        Also: get_stripe_customer_id similarly exposes stripe_customer_id to any
        caller (anon), which directly leaks Stripe customer IDs.
  blast_radius: |
    Any unauthenticated request can enumerate token balances for all users if
    they know (or can guess) UUIDs. More critically, get_stripe_customer_id
    exposes Stripe customer IDs (PII linkage) to anonymous callers.
    Authenticated non-admin users can also look up other users' balances and
    Stripe IDs. With 13 real users in production these are already live IDOR bugs.
  proposed_fix: |
    For check_balance and get_user_tokens: either REVOKE PUBLIC/anon EXECUTE
    (service_role only), or add an ownership or admin guard:
      IF auth.uid() <> target_user_id AND NOT public.is_admin(auth.uid()) THEN
        RAISE EXCEPTION 'access denied';
      END IF;
    For get_stripe_customer_id: same, plus consider removing from public API
    schema entirely since it's only called by the backend service_role path.
    All three also need SET search_path fixes (A1-02).
  fix_type: migration
  test_to_add: |
    test_check_balance_anon_blocked: anon call with a victim UUID → expect 403.
    test_get_stripe_id_anon_blocked: anon call → expect 403.
    test_check_balance_wrong_user_blocked: authenticated user A calls with user B
    UUID → expect 403.
  blocking: [none]
  confidence: high
```

```yaml
- id: A1-04
  severity: P1
  category: security
  surface: db
  title: update_profile_by_id and update_profile_by_stripe_customer have no caller-identity guard — any caller can rewrite subscription fields
  evidence:
    - file: pg_catalog (prosrc + ACL)
      lines: "update_profile_by_id: SECURITY DEFINER, no auth check, PUBLIC EXECUTE; update_profile_by_stripe_customer: same"
      excerpt: |
        CREATE OR REPLACE FUNCTION public.update_profile_by_id(target_user_id uuid, payload jsonb)
         RETURNS void  LANGUAGE plpgsql  SECURITY DEFINER  -- no SET search_path
        AS $function$
        BEGIN
          UPDATE profiles
          SET
            stripe_customer_id = COALESCE(payload->>'stripe_customer_id', stripe_customer_id),
            stripe_subscription_id = COALESCE(payload->>'stripe_subscription_id', stripe_subscription_id),
            subscription_status = COALESCE(payload->>'subscription_status', subscription_status),
            subscription_plan = COALESCE(payload->>'subscription_plan', subscription_plan),
            ...
            plan = COALESCE(payload->>'plan', plan),
            full_name = COALESCE(payload->>'full_name', full_name),
          WHERE id = target_user_id;
        END;
        $function$
        -- ACL: =X/postgres (PUBLIC), anon=X/postgres, authenticated=X/postgres
    - reproduction: |
        POST https://afnbtbeayfkwznhzafay.supabase.co/rest/v1/rpc/update_profile_by_id
        apikey: <anon_key>
        {"target_user_id": "<any_uuid>", "payload": {"subscription_status": "active",
         "subscription_plan": "enterprise", "plan": "enterprise",
         "stripe_customer_id": "cus_attacker"}}
        This grants any user an active enterprise subscription for free.
  blast_radius: |
    Any unauthenticated caller can set subscription_status, subscription_plan,
    stripe_customer_id, and the plan field for any user. This is a direct
    subscription-fraud vector: anonymous users can self-upgrade to paid plans.
    The profiles_owner_update_safe RLS policy blocks direct UPDATE via SQL but
    does not apply to SECURITY DEFINER RPCs, which bypass RLS.
    update_profile_by_stripe_customer similarly allows resetting subscription
    fields for any stripe_customer_id (no auth check, PUBLIC EXECUTE).
  proposed_fix: |
    Both functions must be restricted to service_role only:
      REVOKE ALL ON FUNCTION public.update_profile_by_id(uuid, jsonb) FROM PUBLIC, anon, authenticated;
      REVOKE ALL ON FUNCTION public.update_profile_by_stripe_customer(text, jsonb) FROM PUBLIC, anon, authenticated;
      GRANT EXECUTE ON FUNCTION public.update_profile_by_id(uuid, jsonb) TO service_role;
      GRANT EXECUTE ON FUNCTION public.update_profile_by_stripe_customer(text, jsonb) TO service_role;
    api.py calls these with service_role credentials, so this restriction is safe.
    Also add SET search_path = 'public', 'pg_temp'.
  fix_type: migration
  test_to_add: |
    test_update_profile_by_id_anon_blocked: anon call with any payload → expect 403.
    test_update_profile_by_id_authenticated_blocked: non-admin auth call → expect 403.
    test_update_profile_by_stripe_customer_anon_blocked: same for other function.
  blocking: [none]
  confidence: high
```

```yaml
- id: A1-05
  severity: P2
  category: security
  surface: db
  title: admin_count_profiles and admin_list_profiles use inline auth.uid() check without is_admin helper — inconsistent auth pattern and missing search_path
  evidence:
    - file: pg_catalog (prosrc)
      lines: "admin_count_profiles and admin_list_profiles: SECURITY DEFINER, proconfig=null (no search_path), use inline subquery"
      excerpt: |
        -- admin_count_profiles:
        CREATE OR REPLACE FUNCTION public.admin_count_profiles()
         RETURNS integer  LANGUAGE sql  SECURITY DEFINER
        AS $function$
          SELECT COUNT(*)::integer
          FROM profiles
          WHERE (SELECT role FROM profiles WHERE id = auth.uid()) = 'admin';
        $function$

        -- admin_list_profiles (same pattern):
        SELECT p.id, ...
        FROM profiles p
        WHERE (SELECT role FROM profiles WHERE id = auth.uid()) = 'admin'

        -- These use inline subquery, not public.is_admin() helper.
        -- Also: both have PUBLIC + anon EXECUTE (same exposure class as A1-01)
        -- No SET search_path.
    - reproduction: |
        Anon can call admin_list_profiles() but the WHERE clause guards the result.
        However, the inconsistent pattern means if auth.uid() returns NULL
        (unauthenticated), the subquery returns NULL, which is not equal to 'admin',
        so it correctly returns empty. But the function itself still runs under
        postgres-owner context with PUBLIC access — a defense-in-depth failure.
        A future refactor that changes the null handling could accidentally expose
        the data.
  blast_radius: |
    Currently safe for data exposure due to the subquery NULL guard, but:
    (1) inconsistent with is_admin() used by all other admin RPCs (R4 partially open),
    (2) PUBLIC+anon EXECUTE on a function that lists all user profiles (emails,
    stripe IDs, roles) is an unnecessary attack surface,
    (3) no SET search_path means a search_path injection could shadow the
    profiles table. Also note: these functions are NOT in the "fixed" ACL group
    (admin_set_credits has no PUBLIC grant after 004; these still do).
  proposed_fix: |
    Rewrite both functions to use public.is_admin(auth.uid()) consistently,
    add SET search_path = 'public', 'auth', and:
      REVOKE ALL ON FUNCTION public.admin_count_profiles() FROM PUBLIC, anon;
      REVOKE ALL ON FUNCTION public.admin_list_profiles() FROM PUBLIC, anon;
      GRANT EXECUTE ON ... TO authenticated, service_role;
    This aligns with the pattern applied to admin_set_credits in migration 004.
  fix_type: migration
  test_to_add: |
    test_admin_list_profiles_anon_blocked: after fix, anon call → 403.
    test_admin_list_profiles_non_admin_blocked: authenticated non-admin → empty or 403.
    test_admin_count_profiles_uses_is_admin: pg_get_functiondef should contain 'is_admin'.
  blocking: [none]
  confidence: high
```

```yaml
- id: A1-06
  severity: P2
  category: security
  surface: db
  title: admin_audit_insert is callable by anon/authenticated — fake audit log entries can be injected by any user
  evidence:
    - file: pg_catalog (ACL + prosrc)
      lines: "admin_audit_insert: SECURITY DEFINER, search_path=public,auth, ACL includes anon=X/postgres and =X/postgres (PUBLIC)"
      excerpt: |
        -- admin_audit_insert is guarded internally:
        IF NOT public.is_admin(p_actor_id) THEN
          RAISE EXCEPTION 'not_admin';
        END IF;
        -- But p_actor_id is a CALLER-SUPPLIED uuid, not auth.uid().
        -- An admin user can call this directly with their own UUID and inject
        -- arbitrary audit entries. More importantly: the function ACL
        -- exposes it to anon (though anon will fail the is_admin check).

        -- The real attack surface: if an admin's session is compromised,
        -- they can inject fake "before" states into audit_log to cover tracks.
        -- Also: actor_email is fetched FROM profiles, so the entry is
        -- consistent — but the before/after/metadata payloads are arbitrary.
    - reproduction: |
        Authenticated admin calls:
          POST /rpc/admin_audit_insert
          {"p_actor_id": "<admin_uuid>", "p_action": "credits.adjust",
           "p_target_type": "user", "p_target_id": "<victim_uuid>",
           "p_before": {"tokens": 0}, "p_after": {"tokens": 0}, ...}
        Creates a plausible-looking audit entry with fabricated before/after state.
  blast_radius: |
    Audit log integrity is compromised: any admin can write arbitrary entries
    with fabricated before/after states, making the audit trail untrustworthy
    for forensic purposes. Anon callers can attempt the call (it will fail
    the is_admin check) but the broad ACL is unnecessary attack surface.
    The function should be callable only by service_role or internally by
    other admin_ functions, not directly from PostgREST.
  proposed_fix: |
    Remove admin_audit_insert from the public PostgREST-exposed API by either:
    (a) REVOKE ALL FROM PUBLIC, anon, authenticated; GRANT TO service_role only, OR
    (b) Move it to a non-public schema (internal schema), OR
    (c) Replace p_actor_id parameter with auth.uid() internally so the caller
    cannot forge the actor identity.
    Option (c) is the most valuable: change signature to use auth.uid() internally
    rather than accepting p_actor_id from the caller.
  fix_type: migration
  test_to_add: |
    test_audit_insert_anon_blocked: anon call → 403.
    test_audit_insert_actor_is_caller: verify the function enforces p_actor_id = auth.uid().
  blocking: [none]
  confidence: high
```

```yaml
- id: A1-07
  severity: P2
  category: security
  surface: db
  title: expire_credits callable by anon/authenticated — any user can trigger credit expiry sweep
  evidence:
    - file: pg_catalog (ACL)
      lines: "expire_credits: SECURITY DEFINER, proconfig=[search_path=public,pg_temp], ACL=anon=X/postgres,authenticated=X/postgres (no PUBLIC/anon revoke)"
      excerpt: |
        -- expire_credits ACL (from grants query):
        grantee=anon, routine_name=expire_credits, privilege_type=EXECUTE
        grantee=authenticated, routine_name=expire_credits, privilege_type=EXECUTE

        -- Function acquires advisory lock per user and zeroes remaining_credits
        -- for all expired buckets:
        FOR v_b IN SELECT ... FROM public.credit_buckets
          WHERE expires_at IS NOT NULL AND expires_at <= clock_timestamp()
          AND remaining_credits > 0 ... FOR UPDATE LOOP

        -- Compare: admin_set_credits correctly has NO anon/PUBLIC grant after 004.
    - reproduction: |
        POST https://.../rest/v1/rpc/expire_credits
        apikey: <anon_key>  (no body required)
        Returns count of expired buckets; races with scheduled expiry cron job.
  blast_radius: |
    Any anonymous user can trigger the full credit expiry sweep. While this
    function is designed to be idempotent (can't expire already-expired buckets),
    calling it early causes users to lose credits before the scheduled expiry time.
    With advisory locks, a flood of concurrent calls would also contend on the
    advisory lock hash, potentially causing transaction-level lock waits across
    all users. This is a DoS vector against the credit system. Similarly,
    grant_credits, refund_credits, and spend_credits are callable by anon.
  proposed_fix: |
    REVOKE ALL ON FUNCTION public.expire_credits() FROM anon, authenticated;
    GRANT EXECUTE ON FUNCTION public.expire_credits() TO service_role;
    Same for grant_credits, refund_credits, spend_credits if they are only
    intended to be called by service_role (api.py backend).
    Review: grant_credits and spend_credits are the right replacements for
    add_credits/deduct_credits (R6 fix path) so they must remain service_role
    callable but should NOT be anon-callable.
  fix_type: migration
  test_to_add: |
    test_expire_credits_anon_blocked: anon call → 403.
    test_expire_credits_authenticated_non_admin_blocked: non-admin auth call → 403.
  blocking: [none]
  confidence: high
```

---

## CATEGORY: money

```yaml
- id: A1-08
  severity: P1
  category: money
  surface: db
  title: add_credits and deduct_credits bypass the credit_buckets/credit_transactions ledger — profiles.tokens drifts from ledger (R6 confirmation)
  evidence:
    - file: pg_catalog (prosrc for add_credits, deduct_credits)
      lines: "add_credits: updates profiles.tokens directly; deduct_credits: same — neither touches credit_buckets or credit_transactions"
      excerpt: |
        -- add_credits (prosrc):
        UPDATE profiles
        SET tokens = tokens + p_credits, updated_at = now()
        WHERE id = p_user_id;
        -- No INSERT into credit_buckets.
        -- No INSERT into credit_transactions.

        -- deduct_credits (prosrc):
        new_balance := current_tokens - amount;
        UPDATE profiles SET tokens = new_balance, updated_at = now()
        WHERE id = target_user_id;
        RETURN new_balance;
        -- No UPDATE on credit_buckets.
        -- No INSERT into credit_transactions.

        -- Compare: spend_credits correctly drains buckets and writes tx.
        -- grant_credits correctly creates bucket and writes tx.
    - reproduction: |
        Call add_credits(user_uuid, 100): profiles.tokens += 100,
        but credit_buckets and credit_transactions are unchanged.
        credit_balances view (SUM of remaining_credits from buckets) shows
        a different number than profiles.tokens.
        Call deduct_credits(user_uuid, 50): profiles.tokens -= 50,
        but no spend transaction is recorded.
  blast_radius: |
    Every call to the old add_credits / deduct_credits RPCs (used by api.py
    for non-Stripe flows) silently diverges profiles.tokens from the
    credit_buckets ledger. This is the R3/R6 open issue confirmed at the DB layer.
    The ledger is authoritative for expiry and Stripe grant tracking, but
    profiles.tokens is what api.py reads for balance checks. Any user whose
    credits were granted/spent via the old RPCs (instead of grant_credits/
    spend_credits) has an irreconcilable ledger drift. audit_log is also
    empty (0 rows), confirming the audit trail gap.
  proposed_fix: |
    Deprecate add_credits and deduct_credits. Migrate all callers in api.py
    to grant_credits and spend_credits respectively. Then REVOKE PUBLIC EXECUTE
    on both. The reconcile_ledger.py script already exists to backfill the drift.
    This is the Phase 2 api.py work noted in the shared context (R3/R6).
  fix_type: api_patch
  test_to_add: |
    test_ledger_consistency_after_grant: after grant_credits call, verify
    credit_buckets.remaining_credits sum == profiles.tokens.
    test_add_credits_deprecated: after fix, add_credits call → exception or removed.
  blocking: [A1-01]
  confidence: high
```

```yaml
- id: A1-09
  severity: P2
  category: money
  surface: db
  title: spend_credits balance snapshot is racy — balance_after reflects partial deduction mid-loop
  evidence:
    - file: pg_catalog (prosrc for spend_credits)
      lines: "spend_credits body: v_balance_after fetched inside per-bucket loop, not after all buckets drained"
      excerpt: |
        FOR v_bucket IN ... LOOP
          EXIT WHEN v_remaining <= 0;
          v_take := LEAST(v_bucket.remaining_credits, v_remaining);
          UPDATE public.credit_buckets SET remaining_credits = remaining_credits - v_take WHERE id = v_bucket.id;
          -- v_balance_after is read HERE, after each bucket, not once at the end:
          SELECT COALESCE(SUM(remaining_credits),0) INTO v_balance_after
            FROM public.credit_buckets WHERE user_id = p_user_id;
          INSERT INTO public.credit_transactions (..., balance_after, ...)
            VALUES (..., v_balance_after, ...);
          v_remaining := v_remaining - v_take;
        END LOOP;
        -- If 3 buckets are drained: tx 1 shows balance_after=90, tx 2 shows 60,
        -- tx 3 shows 30. Only the last tx reflects the true final balance.
        -- The intermediate balance_after values are misleading: they show
        -- the remaining sum BEFORE other buckets in this same loop iteration
        -- have been drained.
    - reproduction: |
        User has 3 buckets: [50, 40, 30] credits. Call spend_credits(user, 100).
        Three credit_transactions are inserted with balance_after values:
          tx1: balance_after = 40+30 = 70 (bucket 1 drained, but 2+3 still full)
          tx2: balance_after = 30 (bucket 2 drained)
          tx3: balance_after = 20 (bucket 3 partially drained)
        The balance_after on tx1 and tx2 are intermediate snapshots that don't
        reflect the true post-spend balance.
  blast_radius: |
    Incorrect balance_after entries in credit_transactions corrupt the
    append-only ledger's audit trail. Any retrospective balance reconciliation
    that sums balance_after sequences will produce wrong results. This also
    means the credit_transactions table cannot be used for financial
    reconstruction of a user's balance at a point in time — a key invariant
    for any billing system. Affects every multi-bucket spend operation.
  proposed_fix: |
    Move the balance_after SELECT and INSERT outside the loop, after all
    buckets are drained. Build up a list of (bucket_id, credits) pairs in
    the loop, then INSERT all transactions at once with the final balance:
      -- After loop:
      SELECT COALESCE(SUM(remaining_credits),0) INTO v_balance_after
        FROM public.credit_buckets WHERE user_id = p_user_id;
      -- Then insert all transactions using v_balance_after for all rows.
  fix_type: migration
  test_to_add: |
    test_spend_credits_balance_after_consistent: spend 100 credits across 3
    buckets, verify all resulting credit_transactions have the same final
    balance_after (= initial_balance - 100).
  blocking: [none]
  confidence: high
```

---

## CATEGORY: integrity

```yaml
- id: A1-10
  severity: P2
  category: integrity
  surface: db
  title: credit_buckets and credit_transactions FKs reference auth.users(id) not profiles(id) — CASCADE DELETE bypasses profiles FK
  evidence:
    - file: frontend/supabase/migrations/002_deft_credit_ledger.sql
      lines: "15, 52"
      excerpt: |
        -- credit_buckets:
        user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
        -- credit_transactions:
        user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,

        -- But profiles references auth.users too:
        -- profiles.id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE

        -- So: auth.users DELETE → cascades to profiles AND to credit_buckets/
        -- credit_transactions. But a profiles DELETE is not possible via FK
        -- (profiles.id is a PK, not an FK with ON DELETE action).
        -- The inconsistency: credit_buckets.user_id does NOT reference
        -- profiles(id), so a credit_bucket can exist for a user_id that has
        -- no profiles row (e.g., if profiles INSERT failed in handle_new_user).
    - reproduction: |
        1. Simulate a failed handle_new_user trigger (e.g. duplicate email).
        2. If auth.user is created but profiles row is missing,
           grant_credits(user_id, ...) will succeed (credit_buckets FK allows it)
           but get_my_balance() will work while admin functions that JOIN profiles
           will silently miss the user.
  blast_radius: |
    Edge case: if handle_new_user trigger fails (email constraint, schema change),
    credit buckets can accumulate for a user with no profile row. The credit system
    would work (buckets exist) but admin views (admin_list_profiles) would not
    show the user, and profiles-based token counts would be 0. Low probability
    but an integrity gap worth tracking.
  proposed_fix: |
    Add FK from credit_buckets.user_id → public.profiles(id) ON DELETE CASCADE
    in addition to (or replacing) the auth.users FK. This ensures no credit bucket
    can exist without a corresponding profile row. Alternatively, ensure
    handle_new_user is transactional with auth user creation (already done via
    AFTER INSERT trigger, but the trigger has no rollback protection).
  fix_type: migration
  test_to_add: |
    test_credit_bucket_requires_profile: attempt to insert credit_bucket for a
    user_id that exists in auth.users but not profiles → should fail FK.
  blocking: [none]
  confidence: medium
```

```yaml
- id: A1-11
  severity: P2
  category: integrity
  surface: db
  title: admin_set_credits updates profiles.tokens directly but does not touch credit_buckets ledger — same R3/R6 pattern as add_credits
  evidence:
    - file: pg_catalog (prosrc for admin_set_credits)
      lines: "admin_set_credits body: UPDATE public.profiles SET tokens = v_final"
      excerpt: |
        -- admin_set_credits (migrated in 004, now has search_path and audit):
        UPDATE public.profiles
           SET tokens = v_final, updated_at = now()
         WHERE id = target_user_id;
        -- No credit_buckets update.
        -- No credit_transactions INSERT.
        -- The audit_log entry records the delta, but the ledger is not updated.

        -- admin_adjust_credits has the same gap:
        UPDATE public.profiles SET tokens = v_new, updated_at = NOW() WHERE id = p_target;
        -- Also no ledger write.
    - reproduction: |
        Admin calls admin_set_credits(user_uuid, 500, false):
        - profiles.tokens becomes 500
        - credit_balances view still shows the old bucket sum
        - credit_transactions has no entry for this admin adjustment
        The discrepancy is permanent unless reconcile_ledger.py is run.
  blast_radius: |
    Every admin credit adjustment widens the R3 ledger drift. With 13 users
    and a sparse credit_transactions table (1 row), it is likely that most
    credit state exists only in profiles.tokens with no ledger history.
    This means the credit_transactions table is not a reliable audit trail
    for admin-initiated balance changes. The audit_log does record these
    (post-004 fix), but the financial ledger (credit_transactions) is incomplete.
  proposed_fix: |
    Extend admin_set_credits (and admin_adjust_credits) to also write to
    credit_transactions with type='admin_grant' or 'admin_adjust', and
    update credit_buckets accordingly (e.g. create a special admin_grant bucket
    for set operations, or adjust remaining_credits on existing buckets for
    delta operations). This requires Phase 2 coordination with api.py (R3 fix scope).
  fix_type: migration
  test_to_add: |
    test_admin_set_credits_writes_ledger: after admin_set_credits call, verify
    credit_transactions has a new entry with correct balance_after.
  blocking: [A1-08]
  confidence: high
```

---

## CATEGORY: performance

```yaml
- id: A1-12
  severity: P3
  category: performance
  surface: db
  title: 7 FK columns missing covering indexes — cascade-delete and JOIN scans are sequential
  evidence:
    - file: get_advisors(type=performance) + direct FK/index cross-check query
      lines: "unindexed_foreign_keys advisor findings"
      excerpt: |
        -- Tables and unindexed FK columns (confirmed by both advisor and direct query):
        admin_tasks.assigned_to     → admin_tasks_assigned_to_fkey
        admin_tasks.created_by      → admin_tasks_created_by_fkey
        credit_transactions.bucket_id → credit_transactions_bucket_id_fkey
        feature_flags.updated_by    → feature_flags_updated_by_fkey
        investigations.user_id      → investigations_user_id_fkey  ← high traffic
        messages.investigation_id   → messages_investigation_id_fkey
        system_status.frozen_by     → system_status_frozen_by_fkey

        -- Most critical: investigations.user_id is used in every RLS policy
        -- evaluation (SELECT/INSERT/UPDATE by user_id). No btree index on
        -- investigations(user_id) means every RLS check does a seq scan.
        -- messages.investigation_id is used in RLS subquery too.
    - reproduction: |
        EXPLAIN SELECT * FROM investigations WHERE user_id = '<uuid>';
        → Seq Scan on investigations (113 rows currently, will grow)
  blast_radius: |
    investigations.user_id is the filter column for all 3 RLS policies on the
    investigations table (the core table of the product). Without an index,
    every SELECT/INSERT/UPDATE on investigations triggers a sequential scan.
    At 113 rows it is fast; at 10k+ investigations across 100+ users it will
    become a latency bottleneck. messages.investigation_id has the same issue
    for the RLS subquery. credit_transactions.bucket_id affects the
    expire_credits loop (FOR UPDATE scan on buckets).
  proposed_fix: |
    CREATE INDEX CONCURRENTLY idx_investigations_user_id ON public.investigations(user_id);
    CREATE INDEX CONCURRENTLY idx_messages_investigation_id ON public.messages(investigation_id);
    CREATE INDEX CONCURRENTLY idx_credit_tx_bucket_id ON public.credit_transactions(bucket_id);
    The admin_tasks and feature_flags FK columns are lower priority.
  fix_type: migration
  test_to_add: |
    test_investigations_user_id_indexed: SELECT indexname FROM pg_indexes WHERE
    tablename='investigations' AND indexdef LIKE '%user_id%' — must return a row.
  blocking: [none]
  confidence: high
```

```yaml
- id: A1-13
  severity: P3
  category: performance
  surface: db
  title: 20+ RLS policies use bare auth.uid() / auth.role() calls instead of (SELECT auth.uid()) — re-evaluated per row
  evidence:
    - file: get_advisors(type=performance) — auth_rls_initplan
      lines: "auth_rls_initplan findings for profiles, investigations, messages, conversations, conversation_messages, audit_log, feature_flags, admin_tasks, user_vaults, usage_rollup_daily, vault_secrets"
      excerpt: |
        -- Affected policies (partial list from advisor):
        profiles: "Users can read own profile" (USING auth.uid() = id)
        investigations: all 3 policies
        messages: both policies
        conversations: all 4 policies
        conversation_messages: all 3 policies
        audit_log: audit_log_admin_read
        feature_flags: both policies
        admin_tasks: admin_tasks_admin_all
        user_vaults, vault_secrets, usage_rollup_daily: multiple

        -- Fix pattern: USING (auth.uid() = id) → USING ((SELECT auth.uid()) = id)
    - reproduction: |
        EXPLAIN ANALYZE SELECT * FROM profiles WHERE id = auth.uid();
        -- InitPlan appears in the plan for bare auth.uid() calls.
  blast_radius: |
    For tables with many rows (investigations has 113, conversations 88,
    conversation_messages 191), each auth.uid() call is re-evaluated per row
    rather than once per query. At production scale (1000s of messages),
    this causes measurable latency in every user-facing read. The advisor
    flags this as WARN (not critical), but it compounds with the missing
    FK indexes (A1-12).
  proposed_fix: |
    Systematically replace auth.uid() with (SELECT auth.uid()) and
    auth.role() with (SELECT auth.role()) in all RLS policy definitions.
    This is a standard Supabase optimization — see:
    https://supabase.com/docs/guides/database/postgres/row-level-security#call-functions-with-select
  fix_type: migration
  test_to_add: |
    test_rls_no_initplan: run EXPLAIN on a SELECT from investigations with
    auth context; verify no "InitPlan" node appears for auth.uid() in the plan.
  blocking: [none]
  confidence: high
```

```yaml
- id: A1-14
  severity: P3
  category: performance
  surface: db
  title: profiles_owner_update_safe WITH CHECK has 9 correlated subqueries per UPDATE — O(n) self-joins on every profile write
  evidence:
    - file: pg_policies (profiles_owner_update_safe with_check)
      lines: "profiles_owner_update_safe: with_check contains 9 separate (SELECT p.col FROM profiles p WHERE p.id = auth.uid()) subqueries"
      excerpt: |
        -- with_check (condensed):
        auth.uid() = id
        AND role = (SELECT p.role FROM public.profiles p WHERE p.id = auth.uid())
        AND plan = (SELECT p.plan FROM public.profiles p WHERE p.id = auth.uid())
        AND tokens = (SELECT p.tokens FROM public.profiles p WHERE p.id = auth.uid())
        AND COALESCE(stripe_customer_id, '') = COALESCE((SELECT p.stripe_customer_id FROM public.profiles p WHERE p.id = auth.uid()), '')
        AND COALESCE(stripe_subscription_id, '') = COALESCE((SELECT p.stripe_subscription_id FROM public.profiles p WHERE p.id = auth.uid()), '')
        AND COALESCE(subscription_status, 'none') = COALESCE((SELECT p.subscription_status FROM public.profiles p WHERE p.id = auth.uid()), 'none')
        AND COALESCE(subscription_plan, 'none') = COALESCE((SELECT p.subscription_plan FROM public.profiles p WHERE p.id = auth.uid()), 'none')
        AND COALESCE(subscription_current_period_end::text, '') = COALESCE((SELECT p.subscription_current_period_end::text FROM public.profiles p WHERE p.id = auth.uid()), '')
        AND COALESCE(suspended_at::text, '') = COALESCE((SELECT p.suspended_at::text FROM public.profiles p WHERE p.id = auth.uid()), '')
        AND COALESCE(suspended_reason, '') = COALESCE((SELECT p.suspended_reason FROM public.profiles p WHERE p.id = auth.uid()), '')
        AND COALESCE(admin_notes, '') = COALESCE((SELECT p.admin_notes FROM public.profiles p WHERE p.id = auth.uid()), '')
    - reproduction: |
        EXPLAIN ANALYZE UPDATE profiles SET full_name='test' WHERE id = auth.uid();
        — Will show 9+ index scans on profiles (one per subquery) plus the
          InitPlan overhead of auth.uid() repeated 9 times.
  blast_radius: |
    Every profile UPDATE (e.g., user changes full_name) triggers 9 correlated
    subqueries on the profiles table in addition to the actual UPDATE.
    While profiles is small (13 rows), this is a correctness + performance issue:
    if two concurrent UPDATEs race, the subquery can read a stale committed value
    from a concurrent transaction, silently rejecting a valid update. More
    practically: this pattern should be consolidated into a single CTE or
    OLD/NEW comparison.
  proposed_fix: |
    Rewrite WITH CHECK to use a single subquery that fetches all locked columns
    at once, then compare. Example:
      WITH CHECK (
        auth.uid() = id AND (
          SELECT role = NEW.role AND plan = NEW.plan AND tokens = NEW.tokens ...
          FROM public.profiles WHERE id = auth.uid()
        )
      )
    Or more cleanly: use a SECURITY DEFINER helper function that takes (NEW.*) and
    returns boolean, reducing the 9 subqueries to 1.
  fix_type: migration
  test_to_add: |
    test_profile_update_single_lookup: EXPLAIN output for profile UPDATE should
    show at most 1 profiles index scan for the WITH CHECK evaluation.
  blocking: [none]
  confidence: medium
```

```yaml
- id: A1-15
  severity: P3
  category: performance
  surface: db
  title: property-images storage bucket has broad SELECT policy — allows unauthenticated file listing
  evidence:
    - file: get_advisors(type=security) — public_bucket_allows_listing
      lines: "public_bucket_allows_listing for bucket 'property-images'"
      excerpt: |
        "Public bucket `property-images` has 1 broad SELECT policy on
        `storage.objects` (Property images are publicly accessible),
        allowing clients to list all files. Public buckets don't need
        this for object URL access and it may expose more data than intended."
        -- Remediation: https://supabase.com/docs/guides/database/database-linter?lint=0025_public_bucket_allows_listing
    - reproduction: |
        GET https://afnbtbeayfkwznhzafay.supabase.co/storage/v1/object/list/property-images
        apikey: <anon_key>
        Returns full file listing without authentication.
  blast_radius: |
    Any anonymous client can enumerate all files in the property-images bucket.
    This may expose private document names, user IDs embedded in paths, or
    investigation output filenames. The bucket appears to be from a prior
    FlowVoice/property app (the drop_old_flowvoice_tables migration suggests
    tenant reuse). If investigation PDFs/DOCXs are stored here, their paths
    would be enumerable.
  proposed_fix: |
    Restrict the storage.objects SELECT policy to require authentication
    and ownership, or remove the listing permission while keeping object
    URL access. Review whether this bucket is still needed by the current
    application (it may be a legacy artifact from FlowVoice).
  fix_type: migration
  test_to_add: |
    test_storage_bucket_no_anon_listing: anon GET to /storage/v1/object/list/property-images
    should return empty or 403.
  blocking: [none]
  confidence: high
```

---

## CATEGORY: correctness

```yaml
- id: A1-16
  severity: P3
  category: correctness
  surface: db
  title: Drift — 001_initial_schema.sql creates "Users can update own profile" policy that was superseded by 004 — migration file not updated
  evidence:
    - file: frontend/supabase/migrations/001_initial_schema.sql
      lines: "60-61"
      excerpt: |
        CREATE POLICY "Users can update own profile" ON public.profiles
          FOR UPDATE USING (auth.uid() = id);
        -- This is the WEAK policy that 004_loop5_idempotency_and_rls.sql drops.
        -- Migration 004 has: DROP POLICY IF EXISTS "Users can update own profile" ON public.profiles;
        -- Live state: this policy does NOT exist (correctly dropped).
        -- But 001 still has the CREATE, so a clean re-apply of migrations
        -- would create the weak policy and 004 would drop it again — idempotent,
        -- but confusing and fragile.

        -- Also: 001 defines ticker and hypothesis as NOT NULL:
        --   ticker TEXT NOT NULL,
        --   hypothesis TEXT NOT NULL,
        -- But live investigations table has both as NULLABLE (migration
        -- 20260416092124 make_ticker_hypothesis_nullable ran).
        -- 001 never got updated to reflect this — the migration file is stale.
    - reproduction: |
        Clean apply of 001 would create NOT NULL ticker/hypothesis, then a later
        migration makes them nullable. The file itself misleads anyone reading it
        as the "baseline schema."
  blast_radius: |
    Re-apply from scratch would fail if 001's NOT NULL constraints conflict with
    data inserted under the nullable schema. The dead policy creation adds noise.
    Low operational risk but high documentation/drift risk.
  proposed_fix: |
    Update 001_initial_schema.sql to reflect the current live schema (nullable
    ticker/hypothesis, no weak update policy). Add a comment in 001 noting
    which subsequent migrations override specific DDL.
  fix_type: docs
  test_to_add: |
    test_migrations_idempotent_clean: apply all migrations to a fresh DB in order
    and verify the final schema matches the live schema exactly.
  blocking: [none]
  confidence: high
```

---

## CATEGORY: availability

```yaml
- id: A1-17
  severity: P3
  category: availability
  surface: db
  title: handle_new_user trigger has no SET search_path — mutable search_path during new user signup
  evidence:
    - file: pg_catalog (prosrc + proconfig for handle_new_user) + get_advisors(security)
      lines: "handle_new_user: SECURITY DEFINER, proconfig=null (no search_path), fires on auth.users INSERT"
      excerpt: |
        CREATE OR REPLACE FUNCTION public.handle_new_user()
         RETURNS TRIGGER  LANGUAGE plpgsql  SECURITY DEFINER
        AS $function$
        BEGIN
          INSERT INTO public.profiles (id, email, full_name)
          VALUES (NEW.id, NEW.email, NEW.raw_user_meta_data->>'full_name');
          RETURN NEW;
        END;
        $function$
        -- No SET search_path.
        -- Fires as: CREATE TRIGGER on_auth_user_created AFTER INSERT ON auth.users
        -- Confirmed live: trigger exists (on_auth_user_created, action_statement=
        --   EXECUTE FUNCTION handle_new_user())
        -- Supabase advisor: function_search_path_mutable for handle_new_user
    - reproduction: |
        If a rogue schema object shadows 'profiles' in the search_path during
        signup, new user profile creation silently fails or inserts into the
        wrong table. Since this fires as a trigger (not a direct RPC call),
        the search_path injection vector is lower, but the R5 class of fix
        (SET search_path) should be applied uniformly.
  blast_radius: |
    Low probability but: if triggered with a manipulated search_path (e.g. via
    SET search_path in the same transaction by a concurrent trigger), the INSERT
    could go to a wrong table, silently orphaning the auth user without a profile.
    New user signup would appear to succeed but all subsequent operations that
    require a profiles row would fail. The advisory lock and search_path risk
    apply uniformly.
  proposed_fix: |
    ALTER FUNCTION public.handle_new_user() SET search_path = 'public', 'auth';
    This is the same fix as A1-02 but noted separately because handle_new_user
    is a trigger function (fires on auth schema events) and has different
    exposure than a direct RPC call.
  fix_type: migration
  test_to_add: |
    test_handle_new_user_has_search_path: SELECT proconfig FROM pg_proc WHERE
    proname = 'handle_new_user' AND pronamespace = 'public'::regnamespace
    should return array containing 'search_path=public, auth'.
  blocking: [none]
  confidence: high
```

---

## Methodology gaps (what could not be audited)

The live-state read-only SQL access allowed full inspection of schema, policies, functions, and advisors. However, several audit dimensions remain unverifiable from this lens alone: (1) **Concurrent behavior** — race conditions in spend_credits (two concurrent calls for the same user) and the advisory lock hash collision probability under load cannot be confirmed without a load test; the pg_advisory_xact_lock hash space is 64-bit but hash collisions between different user_ids could cause spurious cross-user serialization. (2) **Stripe webhook ordering** — update_profile_by_stripe_customer processes Stripe events ordered by processed_at, but out-of-order webhook delivery (Stripe does not guarantee order) could result in subscription_status being set to an older value; this requires api.py code inspection (A2 lens). (3) **api.py call sites** — which code paths call add_credits vs grant_credits, deduct_credits vs spend_credits, and whether the deprecated RPCs can be safely removed, requires the A2 lens. (4) **Token-to-credit reconciliation** — the exact magnitude of R3 drift (profiles.tokens vs credit_balances view) is not auditable without reading individual user data; the credit_transactions table has only 1 row while investigations has 113, confirming massive ledger under-recording. (5) **Schema version drift from undocumented live migrations** — the live DB has 30 applied migrations but only 5 SQL files are tracked in the repo's migrations/ folder; the remaining 25 migrations (applied via Supabase dashboard or other tooling) are not in version control, meaning the authoritative schema cannot be reconstructed from the repo alone.
