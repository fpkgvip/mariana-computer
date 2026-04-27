# ADR-B42: Supabase JWT Storage — sessionStorage instead of localStorage

**Status:** Accepted  
**Date:** 2026-04-27  
**Bug ID:** B-42 (A4-09)  
**Author:** Loop-6 zero-bug subagent

---

## Context

Supabase JS client v2 persists the user session (both the short-lived
`access_token` and the 60-day `refresh_token`) in `localStorage` by default,
under the key `sb-{projectRef}-auth-token`.

`localStorage` is readable by any JavaScript executing on the same origin.
This means any XSS vulnerability — regardless of severity — can exfiltrate
the refresh token, enabling persistent session hijacking for up to 60 days
without the user being able to detect or revoke it through normal means.

The deft.computer frontend has several compounding XSS-surface risks catalogued
in the audit:

- **A4-05 / B-26**: Hand-rolled markdown renderer in FileViewer.tsx using
  `dangerouslySetInnerHTML` with unbounded heading patterns.
- **A4-06 / B-27**: PreviewPane iframe using `allow-same-origin`, giving
  user-generated agent output access to the parent frame's localStorage.
- **A4-04 / B-10**: Previously no Content-Security-Policy header (now fixed).

Even with those mitigations in place, defense-in-depth requires reducing the
value of a successful localStorage read.

---

## Decision

Switch the Supabase client's storage backend from `localStorage` to a custom
`SupportedStorage` adapter that wraps `sessionStorage`.

```typescript
const SESSION_STORAGE: SupportedStorage = {
  getItem:    (key) => sessionStorage.getItem(key),
  setItem:    (key, value) => sessionStorage.setItem(key, value),
  removeItem: (key) => sessionStorage.removeItem(key),
};

createClient(url, anonKey, {
  auth: {
    storage: SESSION_STORAGE,
    autoRefreshToken: true,
    persistSession: true,
  },
});
```

---

## Trade-offs

### Accepted costs

| Behaviour | Details |
|-----------|---------|
| Per-tab sessions | Each browser tab has its own isolated `sessionStorage`. Opening a new tab does not inherit the parent tab's session. The user must sign in again in the new tab (or Supabase's silent token refresh may succeed if the new tab navigates to the app while the originating tab's session is still active — this depends on the Supabase refresh-token endpoint). |
| Lost on tab close | The session is cleared when the tab is closed. On next visit the user will see the login page rather than being auto-signed-in. |
| Iframe access | `sessionStorage` is isolated per browsing context; iframes with `allow-same-origin` can only read their own `sessionStorage`, not the parent's. |

### Retained benefits

| Behaviour | Details |
|-----------|---------|
| Within-tab persistence | `persistSession: true` keeps the session alive across page reloads within the same tab. |
| Auto-refresh | `autoRefreshToken: true` silently renews the access token before it expires (15-minute window) so the user is not interrupted during normal use. |
| Clean logout | `sessionStorage.removeItem` is called by the Supabase client on `auth.signOut()`, leaving no stale tokens. |

---

## Rejected alternatives

### 1. Memory-only storage (`{}` object)

A plain in-memory object (no `getItem`/`setItem` persistence) would give the
smallest possible attack surface — no token survives a page reload. However,
users would need to sign in after every reload, which is unacceptable UX for
a productivity tool like deft.computer.

### 2. HttpOnly cookie wrapper (server-side proxy)

The gold-standard solution: store the refresh token in an HttpOnly cookie via
a thin server-side `/auth/refresh` proxy. The cookie cannot be read by any JS.
Deferred because it requires a new backend route, CSRF protection, and changes
to the Supabase session management flow. This is the recommended long-term
migration target.

### 3. Keep localStorage with CSP + iframe isolation

Relying solely on A4-04 (CSP) and A4-06 (iframe origin isolation) to prevent
XSS does not eliminate the risk; it only reduces the probability of successful
exploitation. A defense-in-depth approach requires reducing the damage radius
even if those mitigations are bypassed.

---

## Required follow-ups

- [ ] **B-27 Option A**: Move preview iframe to `preview.deft.computer` subdomain to
  eliminate `allow-same-origin` as an XSS vector (see B-27 registry entry).
- [ ] **Long-term**: Implement HttpOnly cookie proxy for the refresh token to
  fully eliminate client-side token storage.
- [ ] **UX monitoring**: Track user-reported session loss complaints after this
  change. If per-tab isolation causes friction, revisit the memory-only option
  paired with a silent background re-authentication flow.

---

## References

- [Supabase Auth Storage docs](https://supabase.com/docs/reference/javascript/auth-overview#custom-storage-adapter)
- A4-09 finding in `loop6_audit/A4_frontend.md`
- B-42 entry in `loop6_audit/REGISTRY.md`
