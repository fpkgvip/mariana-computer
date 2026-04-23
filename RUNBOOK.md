# Mariana v3.7 — Runbook

## Endpoints
- **Frontend:** https://frontend-tau-navy-80.vercel.app
- **Backend API:** http://77.42.3.206:8080
- **Supabase:** https://afnbtbeayfkwznhzafay.supabase.co (project ID `afnbtbeayfkwznhzafay`, region `ap-southeast-2`)

## Admin panel
- URL: `${FRONTEND_URL}/admin` (e.g. https://frontend-tau-navy-80.vercel.app/admin)
- Log in as `fpkgvip@gmail.com` (role `admin` on profiles, also the `ADMIN_USER_ID` env var).
- 10 tabs: Overview, Users, Tasks, Usage & Costs, Models, Feature Flags, Audit Log, System Health, Admin Todo, Danger Zone.
- Deep links: `/admin#users`, `/admin#audit`, etc.

## Backend endpoints (admin)
Base: `${API}/api/admin`

| Method | Path                              | Purpose                                           |
| ------ | --------------------------------- | ------------------------------------------------- |
| GET    | `/overview`                       | Live dashboard stats                              |
| GET    | `/users`                          | List all users                                    |
| POST   | `/users/{id}/role`                | Set role (`user`/`admin`/`banned`)                |
| POST   | `/users/{id}/suspend`             | Suspend/unsuspend                                 |
| POST   | `/users/{id}/credits-v2`          | Adjust credits (set/delta, audited)               |
| GET    | `/tasks`                          | List user tasks (investigations)                  |
| GET    | `/audit-log`                      | Audit entries                                     |
| GET\|POST\|DELETE | `/feature-flags[/ {key}]` | CRUD on flags                                |
| GET\|POST\|PATCH\|DELETE | `/admin-tasks[/{id}]` | Internal todo list                        |
| GET    | `/usage`                          | Daily usage rollup                                |
| GET    | `/health-probe`                   | Deep health check                                 |
| POST   | `/system/freeze`                  | Global kill-switch                                |
| POST   | `/danger/flush-redis`             | Flush Redis (requires phrase `I UNDERSTAND`)      |
| POST   | `/danger/halt-running`            | Halt all RUNNING tasks                            |

All admin endpoints:
- Require `Authorization: Bearer <jwt>` from Supabase auth
- Are double-gated: FastAPI `_require_admin` + Postgres SECURITY DEFINER RPC that re-checks role
- Are audited via `admin_audit_insert` (see Audit Log tab)

## Supabase DB
### New tables (v3.7)
- `audit_log`                — every admin mutation
- `feature_flags`            — per-flag enable/disable + JSON value
- `admin_tasks`              — internal todo list (CTO-owned)
- `usage_rollup_daily`       — daily per-user usage aggregates
- `system_status`            — single row; holds kill-switch state + message

### Admin RPC functions (SECURITY DEFINER)
All take `caller UUID` as first argument and verify admin role:
- `is_admin(uuid) -> bool`
- `admin_audit_insert(...)`
- `admin_audit_list(caller, limit_n, offset_n, action_filter)`
- `admin_set_role(caller, target, new_role)`
- `admin_suspend(caller, target, suspend_bool, reason)`
- `admin_adjust_credits(caller, target, mode, amount, reason) -> int`
- `admin_system_freeze(caller, frozen_bool, reason, message)`
- `admin_list_tasks(caller, status_filter, user_id_filter, limit_n, offset_n)`
- `admin_overview_stats(caller) -> jsonb`

## Deploy procedure
### Backend (FastAPI)
```bash
# 1. Copy updated file to host
scp -i ~/.ssh/hetzner_deploy mariana/api.py root@77.42.3.206:/tmp/api.py

# 2. SSH in and copy to both containers + restart
ssh -i ~/.ssh/hetzner_deploy root@77.42.3.206
docker cp /tmp/api.py mariana-api:/app/mariana/api.py
docker cp /tmp/api.py mariana-orchestrator:/app/mariana/api.py
docker restart mariana-api mariana-orchestrator

# 3. Verify
curl -fsS http://localhost:8080/api/health
```

### Frontend (React/Vite on Vercel)
```bash
cd frontend
# Option A: push to GitHub — Vercel auto-deploys
git add -A && git commit -m "..." && git push origin main

# Option B: manual
VERCEL_TOKEN=... npx vercel --prod --token $VERCEL_TOKEN
```

### Smoke after deploy
```bash
# Get a fresh admin JWT (browser devtools → supabase.auth.getSession())
export MARIANA_ADMIN_TOKEN='eyJ...'
python3 debug_probe.py --api http://77.42.3.206:8080 --json probe.json
```
Exit code 0 means all RED checks passed.  Yellow warnings are acceptable
but should be investigated.

## Rollback
### Backend
```bash
ssh -i ~/.ssh/hetzner_deploy root@77.42.3.206
cd /opt/mariana
# Git tag each release; to revert:
git checkout <previous-tag> -- mariana/api.py
docker cp mariana/api.py mariana-api:/app/mariana/api.py
docker cp mariana/api.py mariana-orchestrator:/app/mariana/api.py
docker restart mariana-api mariana-orchestrator
```
### Frontend
On Vercel dashboard → Deployments → select previous build → "Promote to
Production".  Or `vercel rollback <deployment-url>`.

### Database migrations
No down-migrations are shipped.  Rollback = manual DROP (see
`/home/user/workspace/mariana_v3.7_admin_migration_down.sql` if checked
in).  The v3.7 admin schema is purely additive — existing queries keep
working without it.

## Secrets (checked-in location: `.env` on host `/opt/mariana/.env`)
- `ADMIN_USER_ID=a34a319e-a046-4df2-8c98-9b83f6d512a0`
- `SUPABASE_URL`, `SUPABASE_ANON_KEY` — required
- `LLM_GATEWAY_BASE_URL`, `LLM_GATEWAY_API_KEY` — required
- `SANDBOX_SHARED_SECRET` — required
- `ADMIN_SECRET_KEY` — optional; enables `POST /api/shutdown`
- `STRIPE_*` — billing

## On-call triage
1. Check **Overview** tab — if `frozen=true`, the kill-switch was engaged.
2. Check **System Health** — each dependency probe shows `latency_ms` + `detail`.
3. Check **Audit Log** — look for recent `danger.*` or `system.freeze` entries.
4. For stuck tasks: **Tasks** tab → filter by status=RUNNING, inspect IDs;
   if needed, **Danger Zone → Halt RUNNING tasks**.
5. For runaway cost: **Usage & Costs** → top-10 by spend; consider suspending
   the offending user via **Users** tab.
