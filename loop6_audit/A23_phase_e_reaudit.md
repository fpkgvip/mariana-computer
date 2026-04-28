# Phase E re-audit #18

Header: model=gpt_5_4, commit=44b3c8e, scope=focused on T-01 fix correctness + ledger idempotency + ref_type collision + test adequacy + quick broader sweep.

## Surface walkthrough
### Probe 1 — T-01 fix correctness
- In `_settle_agent_credits`, the pre-RPC claim lookup now reads `completed_at` and `ledger_applied_at` from `agent_settlements`; if `completed_at` is already set it short-circuits with `task.credits_settled = True`, and if only `ledger_applied_at` is set it first calls `_mark_settlement_completed(...)` and only then flips `task.credits_settled = True` (`loop.py:533-567`).
- The noop branch (`delta == 0`) also delays `task.credits_settled = True` until after `_mark_settlement_completed(...)` succeeds; on marker failure it returns with the in-memory flag still false (`loop.py:639-661`).
- After a successful live RPC, the post-RPC path now does `_mark_ledger_applied(...)` first, then `_mark_settlement_completed(...)`, and only after both succeed does it assign `task.credits_settled = True` (`loop.py:776-817`). I did not find a settlement-path assignment that flips the in-memory flag before durable row state reflects applied-ledger or completed settlement.
- `_mark_settlement_completed(...)` stamps both `ledger_applied_at = COALESCE(ledger_applied_at, now())` and `completed_at = now()` in one statement (`loop.py:407-428`), while `_mark_ledger_applied(...)` separately stamps `ledger_applied_at` under an `IS NULL` guard (`loop.py:431-448`).
- In the reconciler, candidate selection now returns `ledger_applied_at`; rows with `ledger_applied_at IS NOT NULL` go down the marker-fixup path that calls only `_mark_settlement_completed(...)` and never `_settle_agent_credits(...)` (`settlement_reconciler.py:92-108, 138-158`). Rows with `ledger_applied_at IS NULL` are the only ones that force `task.credits_settled = False` before retrying `_settle_agent_credits(...)` (`settlement_reconciler.py:175-187`).
- One caveat outside the durable-DB path: `_settle_agent_credits` still sets `task.credits_settled = True` immediately when Supabase wiring is absent (`loop.py:511-520`). That branch has no durable fence, but it is a deliberate "service unavailable / nothing to settle" escape hatch rather than the settlement-replay surface that triggered T-01.
- Conclusion: Probe 1 passes for the intended DB-backed settlement path. I found no remaining callsite where `task.credits_settled = True` is assigned before durable state records ledger application or completion for the same task.

### Probe 2 — grant_credits / refund_credits idempotency on LIVE
- LIVE function signatures on project `afnbtbeayfkwznhzafay` are: `grant_credits(p_user_id uuid, p_credits integer, p_source text, p_ref_type text DEFAULT NULL, p_ref_id text DEFAULT NULL, p_expires_at timestamptz DEFAULT NULL)`, `refund_credits(p_user_id uuid, p_credits integer, p_ref_type text, p_ref_id text)`, and `spend_credits(p_user_id uuid, p_credits integer, p_ref_type text, p_ref_id text, p_metadata jsonb DEFAULT '{}'::jsonb)`.
- LIVE `grant_credits` is idempotent by a SELECT-first check: after taking `pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0))`, it queries `credit_transactions` for `type = 'grant' AND ref_type = p_ref_type AND ref_id = p_ref_id` and returns `status='duplicate'` if present before inserting any bucket or transaction.
- LIVE `refund_credits` is likewise SELECT-first idempotent: after the same per-user advisory lock, it checks for an existing `credit_transactions` row with `type = 'refund' AND ref_type = p_ref_type AND ref_id = p_ref_id`, and also checks `credit_clawbacks` for the same `(ref_type, ref_id)`, returning `status='duplicate'` if either already exists.
- LIVE `spend_credits` accepts the keyed arguments but has no corresponding duplicate check before inserting spend transactions.
- Important: I verified there is no UNIQUE/EXCLUDE constraint on `credit_transactions` or `credit_buckets` in LIVE NestD. The function-level idempotency exists for `grant_credits` and `refund_credits`, but it is not backed by a table constraint.
- Conclusion: T-01 is not regressed on LIVE for the paths under audit because the settlement code calls `grant_credits` / `refund_credits`, and both functions do perform keyed duplicate suppression before mutating ledger state. `spend_credits` remains non-idempotent, but agent settlement does not use it.

### Probe 3 — ref_type collision sweep
- The new agent settlement code uses exactly two `p_ref_type` values: `agent_task_overrun` on the overrun/debit path to `refund_credits` and `agent_task` on the refund path to `grant_credits` (`loop.py:681-739`).
- A repo-wide grep over Python code found no other caller using either `agent_task_overrun` or `agent_task` as a ledger `ref_type`; the only other concrete `ref_type` literal I found in active application code was Stripe’s `stripe_event` in the grant path (`api.py:6121-6129`).
- A repo-wide grep over migrations found no SQL caller or function branch using `agent_task` or `agent_task_overrun` as existing business keys. The matches were confined to the agent loop comments/tests and prior audit notes.
- Conclusion: no `ref_type` collision found for the newly introduced agent settlement keys.

### Probe 4 — T-01 test adequacy
- The regression test patches `_mark_settlement_completed` to fail once and then delegate to the real implementation, which matches the original bug window: successful ledger RPC followed by marker-write failure (`test_t01_marker_loss_no_replay.py:206-220`, `329-341`).
- It records RPC POSTs through the scripted `httpx.AsyncClient` stand-in and asserts exactly one POST after the first settle attempt and still exactly one after reconciler execution, so it does directly guard against replayed ledger calls (`test_t01_marker_loss_no_replay.py:202-260`, `323-365`).
- It exercises both required paths: refund (`delta < 0`) with reserved 500 / spent 0.30 and overrun (`delta > 0`) with reserved 500 / spent 6.00 (`test_t01_marker_loss_no_replay.py:198-200`, `318-320`).
- Minor adequacy note only: the terminal-row assertion accepts `completed_at IS NOT NULL OR ledger_applied_at IS NOT NULL`, which is slightly looser than the intended final state after reconciler fix-up, but it does not undermine the core no-replay guarantee this test is meant to enforce.
- Conclusion: the T-01 regression test is materially adequate and fails at the right moment in the workflow.

### Probe 5 — quick broader sweep
- In the Stripe webhook dispatcher, `customer.subscription.deleted`, `charge.refunded`, `charge.dispute.created`, and `charge.dispute.funds_withdrawn` are all routed to dedicated handlers; I found no `customer.deleted` or `invoice.payment_failed` branch in the current dispatcher snippet (`api.py:5688-5710`).
- The `customer.subscription.deleted` handler immediately patches the profile to `subscription_status='canceled'` and `plan='free'` when a Stripe customer ID is present (`api.py:6295-6322`).
- The refund/dispute reversal path is centralized in `_reverse_credits_for_charge(...)`, which states that dedup check, already-reversed summation, `refund_credits` call, and dedup-row insert all run inside the `process_charge_reversal` SECURITY DEFINER function (`api.py:6579-6596`), and the two webhook entry points call that helper for `charge.refunded` and `charge.dispute.created` (`api.py:6741-6787`).
- `_authenticate_supabase_token(...)` does not do a local JWT issuer comparison; instead it delegates validation to `GET /auth/v1/user` on the configured Supabase project and rejects non-200 responses (`api.py:1216-1255`). I saw no separate issuer-check regression in this snippet.
- `git diff --name-only 5e212e1..44b3c8e -- 'frontend/supabase/migrations/*.sql'` returned no changed migration files, so there are no new SECURITY DEFINER SQL functions in this commit range to review for `SET search_path = ''` posture.
- Conclusion: no additional concrete bug surfaced in the timeboxed broader sweep.

## Findings

## No findings
Probe 1: Settlement-path `credits_settled=True` flips only after durable marker state, and reconciler marker-fixup does not replay ledger RPCs.
Probe 2: LIVE `grant_credits` and `refund_credits` are functionally idempotent on keyed refs; agent settlement uses them, not `spend_credits`.
Probe 3: No `ref_type` collision found for `agent_task` or `agent_task_overrun`.
Probe 4: The regression test fails the right post-RPC marker path, asserts exactly one POST, and covers both refund and overrun.
Probe 5: Broader sweep found no new exploitable regression in the requested surfaces.

RE-AUDIT #18 COMPLETE findings=0 file=loop6_audit/A23_phase_e_reaudit.md
