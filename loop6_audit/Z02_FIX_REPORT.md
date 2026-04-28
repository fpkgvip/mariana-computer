# Z-02 Fix Report — Stripe checkout redirect-host allowlist

Status: **FIXED 2026-04-28**
Severity: P2 (production-frontend checkout broken — direct revenue loss)
Branch: `loop6/zero-bug`

## Bug

Phase E re-audit #28 (A33) found that
`mariana/api.py:create_checkout` validates `success_url` and
`cancel_url` against an inline `_ALLOWED_REDIRECT_HOSTS` set that
contained only `frontend-tau-navy-80.vercel.app`, `localhost`, and
`127.0.0.1`. The production frontend host `app.mariana.computer` —
already in the CORS list at `_DEFAULT_PROD_CORS_ORIGINS` (api.py:452)
— was missing from this allowlist. Any production checkout request
with `success_url=https://app.mariana.computer/checkout/return` was
rejected with HTTP 400 `Invalid success_url: host
'app.mariana.computer' is not allowed`. No checkout / subscription /
top-up flow worked from production.

## Fix

Smallest blast radius and single source of truth: derive
`_ALLOWED_REDIRECT_HOSTS` from the same `_DEFAULT_PROD_CORS_ORIGINS` /
`_DEFAULT_DEV_CORS_ORIGINS` lists used by the CORS middleware, plus an
explicit retention of loopback hosts for dev workflows whose ports do
not appear in the dev CORS list:

```python
from urllib.parse import urlparse
_ALLOWED_REDIRECT_HOSTS: set[str] = set()
for _origin in (*_DEFAULT_PROD_CORS_ORIGINS, *_DEFAULT_DEV_CORS_ORIGINS):
    try:
        _h = urlparse(_origin).hostname
        if _h:
            _ALLOWED_REDIRECT_HOSTS.add(_h)
    except Exception:
        continue
_ALLOWED_REDIRECT_HOSTS.update({"localhost", "127.0.0.1"})
```

A future addition to `_DEFAULT_PROD_CORS_ORIGINS` (e.g. a new custom
domain) is now automatically honoured by checkout. The two surfaces
stay in lockstep without manual duplication.

The downstream check at line 5553 — `if parsed.hostname not in
_ALLOWED_REDIRECT_HOSTS: raise HTTPException(400, ...)` — is unchanged.
Open-redirect protection (VULN-C2-03) still holds because the
allowlist is closed-set.

## TDD trace

### RED at `3cfeab3`

```
$ python -m pytest tests/test_z02_stripe_redirect_allowlist.py -x
HTTPException: 400: Invalid success_url: host 'app.mariana.computer'
is not allowed
```

### GREEN after fix

```
$ python -m pytest tests/test_z02_stripe_redirect_allowlist.py -x
4 passed in 1.96s

$ python -m pytest --tb=short
400 passed, 13 skipped
```

## Regression tests

`tests/test_z02_stripe_redirect_allowlist.py` pins:

1. `test_z02_production_frontend_host_accepted` —
   `success_url=https://app.mariana.computer/checkout/return` is
   accepted; `Stripe.checkout.Session.create` is called and the
   response carries the session URL.
2. `test_z02_attacker_host_rejected` —
   `success_url=https://attacker.example.com/...` still raises 400
   (open-redirect protection regression check).
3. `test_z02_localhost_dev_host_accepted` — `localhost` and
   `127.0.0.1` both work for dev (covered by the explicit retention
   plus the dev CORS list).
4. `test_z02_allowlist_source_includes_production_host` — source-level
   pin asserting `app.mariana.computer` appears in
   `create_checkout` source so a future refactor cannot silently
   drop production support.

## Out of scope

- Stripe Customer Portal (`billing_portal.Session.create`) does not
  accept a redirect URL from the request body, so it is unaffected.
- The CORS middleware's behaviour is unchanged.
- No new env var is introduced; existing `CORS_ALLOWED_ORIGINS`
  override (BUG-027) still applies — operators who set their own CORS
  origins via env var get those origins in the allowlist
  automatically when this code reads `_DEFAULT_PROD_CORS_ORIGINS`. (If
  a deployment overrides `CORS_ALLOWED_ORIGINS` env var without
  including `app.mariana.computer`, that is a deploy-config concern,
  not a regression.)
