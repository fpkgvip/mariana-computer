# A4 — Frontend Audit

## Summary

| Severity | Count |
|----------|-------|
| P0       | 1     |
| P1       | 3     |
| P2       | 4     |
| P3       | 3     |
| P4       | 1     |
| **Total**| **12**|

---

## Findings

```yaml
- id: A4-01
  severity: P0
  category: security
  surface: frontend
  title: Billing portal redirect (Account.tsx) trusts server URL without validation — open redirect to arbitrary domain
  evidence:
    - file: /home/user/workspace/mariana/frontend/src/pages/Account.tsx
      lines: 226-229
      excerpt: |
        const data: { portal_url: string } = await res.json();
        if (!data.portal_url) throw new Error("No portal URL received from server");
        if (popup) popup.location.href = data.portal_url;
        else window.location.href = data.portal_url;
    - reproduction: |
        Contrast with Checkout.tsx (lines 103–114) which validates:
          const parsed = new URL(data.checkout_url);
          const isSameOrigin = parsed.origin === window.location.origin;
          const isStripe = parsed.hostname.endsWith(".stripe.com");
          if (!isSameOrigin && !isStripe) throw new Error("Untrusted checkout URL");
        Account.tsx's handleManageSubscription performs NO such check on portal_url.
        If the backend /api/billing/portal is compromised or returns a crafted response
        (e.g., via SSRF, supply-chain attack on api.py, or a future bug), the frontend
        will redirect the user to an arbitrary URL with no warning.
  blast_radius: |
    Every authenticated user who clicks "Manage billing" is subject to open redirect
    to an attacker-controlled page. Because the frontend pops a window first
    (window.open("", "_self")) and then sets its location, a phishing page would
    have window.opener access if popup semantics are browser-version-dependent.
    The /api/billing/portal route is protected only by the Bearer JWT, so any
    future SSRF or logic bug in api.py's Stripe session creation could weaponise
    this path without additional client-side friction.
  proposed_fix: |
    Copy the same allow-list pattern already used in Checkout.tsx (~lines 103-114).
    Before executing `window.location.href = data.portal_url`, add:
      const parsed = new URL(data.portal_url);
      const isSameOrigin = parsed.origin === window.location.origin;
      const isStripe = parsed.hostname.endsWith(".stripe.com");
      if (!isSameOrigin && !isStripe) {
        toast.error("Invalid billing portal URL received from server");
        setIsOpeningPortal(false);
        return;
      }
  fix_type: frontend_patch
  test_to_add: |
    "billing-portal-open-redirect": mock /api/billing/portal to return
    { portal_url: "https://evil.example.com" }; assert Account.tsx does NOT
    navigate to that URL and shows an error toast instead.
  blocking: [none]
  confidence: high

- id: A4-02
  severity: P1
  category: money
  surface: frontend
  title: Navbar credit display reads stale user.tokens (profiles snapshot) — never auto-refreshes after spend or webhook
  evidence:
    - file: /home/user/workspace/mariana/frontend/src/components/Navbar.tsx
      lines: 126, 203
      excerpt: |
        {user.tokens.toLocaleString()} credits   // line 126
        {user.name} · {user.tokens.toLocaleString()} credits  // line 203
    - file: /home/user/workspace/mariana/frontend/src/contexts/AuthContext.tsx
      lines: 84
      excerpt: |
        tokens: profile?.tokens ?? 0,   // set once on session sync; not re-read on focus
    - reproduction: |
        1. User opens /chat. Navbar shows 5 000 credits.
        2. Agent run completes and deducts 1 200 credits server-side.
        3. Chat.tsx calls refreshUser() which re-fetches profiles.tokens —
           so Chat's user.tokens updates.
        4. But Navbar reads the same AuthContext user object and re-renders only
           when user reference changes from refreshUser(). If refreshUser() fails
           silently (console.warn path at Chat.tsx:1566 etc.), Navbar remains stale.
        5. /build (Build.tsx) uses useCredits() hook hitting /api/credits/balance —
           that IS live. But Navbar does not use useCredits(); it only uses user.tokens.
        6. BuyCredits.tsx (line 47) also shows user.tokens with no refresh.
  blast_radius: |
    Users see an inflated credit balance in the Navbar after every spend event
    until they reload or trigger a successful refreshUser(). Under R3/R6 ledger
    drift (known open issues), the displayed value may already differ from the
    real ledger; the stale Navbar reading compounds this drift visually. Low-credit
    warnings in Chat.tsx (lines 3196–3218) are driven by the same stale user.tokens,
    so "Low credits" or "out of credits" warnings can appear or disappear
    incorrectly, leading users to over- or under-spend.
  proposed_fix: |
    Either: (a) replace user.tokens in Navbar with useCredits() hook (already used
    by Build.tsx), which hits /api/credits/balance and subscribes to the
    deft:credits-changed DOM event, or (b) add a refetchOnWindowFocus path to
    refreshUser() so the profile re-fetches whenever the tab regains focus.
    Option (a) is preferred because it is independent of the AuthContext refresh
    chain and avoids the profile-fetch retry loop on every focus.
    BuyCredits.tsx (line 47) needs the same fix.
  fix_type: frontend_patch
  test_to_add: |
    "navbar-credit-stale": render Navbar with a user whose tokens=5000; fire
    deft:credits-changed event; assert displayed credits update to the mock
    balance returned by /api/credits/balance, not the stale 5000.
  blocking: [none]
  confidence: high

- id: A4-03
  severity: P1
  category: security
  surface: frontend
  title: JWT access token exposed in SSE query string as fallback when stream-token mint fails
  evidence:
    - file: /home/user/workspace/mariana/frontend/src/pages/Chat.tsx
      lines: 1648-1698
      excerpt: |
        let streamToken = token; // Fallback to JWT if mint fails
        try {
          const res = await fetch(`.../stream-token`, { ... });
          ...
          streamToken = data.stream_token;
        } catch {
          // Fallback: use JWT directly (backward compat with older backends)
          ...
        }
        const url = `.../logs?token=${encodeURIComponent(streamToken)}`;
    - file: /home/user/workspace/mariana/frontend/src/components/agent/AgentTaskView.tsx
      lines: 83-86
      excerpt: |
        const url = `${apiUrl}/api/agent/${encodeURIComponent(taskId)}/stream?token=${encodeURIComponent(
          token   // this is the full JWT, not a short-lived stream token
        )}`;
        const es = new EventSource(url);
    - reproduction: |
        AgentTaskView.tsx uses openAgentStream() from agentRunApi.ts which always
        puts the full JWT in the URL (no stream-token mint). Chat.tsx has the
        stream-token path for investigations but falls back to the full JWT on
        any mint error (network error, 5xx, timeout).
        The JWT lands in: nginx access logs, CDN logs, Referer headers if the
        preview iframe navigates away, browser history.
  blast_radius: |
    The Supabase JWT (15-minute expiry) appears in server logs for every SSE
    connection. On the Vercel CDN path the rewrite rule forwards /api/* to
    a bare IP (77.42.3.206:8080), meaning requests hit a self-hosted server whose
    access-log configuration is unknown. Any log aggregation or log-shipping
    mistake would expose live tokens. AgentTaskView.tsx has no short-token path
    at all. Affects every Build-page task run.
  proposed_fix: |
    Extend the stream-token mint pattern from Chat.tsx to AgentTaskView.tsx and
    agentRunApi.ts openAgentStream(). Additionally, remove the fallback-to-JWT
    path in Chat.tsx: if the stream-token endpoint fails, surface an error rather
    than silently downgrading to full-JWT. Long-term, switch EventSource to
    fetch()+ReadableStream with proper Authorization header support.
  fix_type: frontend_patch
  test_to_add: |
    "sse-no-jwt-in-url": mock /stream-token to return 500; assert that
    AgentTaskView and Chat SSE connections are NOT established with the full
    JWT in the URL; assert an error state is shown instead.
  blocking: [none]
  confidence: high

- id: A4-04
  severity: P1
  category: security
  surface: frontend
  title: No Content-Security-Policy header — vercel.json ships no security headers whatsoever
  evidence:
    - file: /home/user/workspace/mariana/frontend/vercel.json
      lines: 1-18
      excerpt: |
        {
          "buildCommand": "npm run build",
          "outputDirectory": "dist",
          "framework": "vite",
          "rewrites": [
            { "source": "/api/:path*", "destination": "http://77.42.3.206:8080/api/:path*" },
            { "source": "/preview/:path*", "destination": "http://77.42.3.206:8080/preview/:path*" },
            { "source": "/(.*)", "destination": "/index.html" }
          ]
        }
    - file: /home/user/workspace/mariana/frontend/index.html
      lines: 1-29
      excerpt: |
        <!doctype html>
        <html lang="en">
          <head>
            <!-- No <meta http-equiv="Content-Security-Policy"> tag -->
            ...
          </head>
        </html>
    - reproduction: |
        curl -I https://deft.computer/ and observe: no Content-Security-Policy,
        no X-Frame-Options, no X-Content-Type-Options, no Strict-Transport-Security,
        no Permissions-Policy.
  blast_radius: |
    Without a CSP, any XSS vector (including future regressions in the hand-rolled
    markdown renderer or AI-generated content paths) has unrestricted script
    execution. The app uses dangerouslySetInnerHTML in FileViewer.tsx and Chat.tsx;
    the only XSS protection today is the hand-rolled HTML-escape pass at the start
    of renderMarkdownImpl — a single regex mistake would be undetected by the browser.
    No X-Frame-Options means the app can be iframed for clickjacking. No HSTS means
    first-visit over HTTP degrades without upgrade. The preview iframe (PreviewPane)
    embeds arbitrary user-deployed apps with allow-same-origin, making CSP
    especially critical here.
  proposed_fix: |
    Add a `headers` block to vercel.json:
      "headers": [{
        "source": "/(.*)",
        "headers": [
          { "key": "Content-Security-Policy", "value": "default-src 'self'; script-src 'self' 'unsafe-inline' https://fonts.googleapis.com; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data: blob:; connect-src 'self' https://*.supabase.co wss://*.supabase.co https://77.42.3.206:8080; frame-src 'self' https://*.stripe.com;" },
          { "key": "X-Frame-Options", "value": "SAMEORIGIN" },
          { "key": "X-Content-Type-Options", "value": "nosniff" },
          { "key": "Strict-Transport-Security", "value": "max-age=63072000; includeSubDomains; preload" },
          { "key": "Permissions-Policy", "value": "camera=(), microphone=(), geolocation=()" }
        ]
      }]
    The preview iframe requires frame-ancestors relaxation or a separate origin.
  fix_type: config
  test_to_add: |
    "security-headers-present": integration test that GETs the root URL and
    asserts the response has CSP, X-Frame-Options, HSTS, and X-Content-Type-Options.
  blocking: [none]
  confidence: high

- id: A4-05
  severity: P2
  category: security
  surface: frontend
  title: renderMarkdownContent (FileViewer) does not sanitize link href — XSS possible via a3://: URI if markdown link pattern is present
  evidence:
    - file: /home/user/workspace/mariana/frontend/src/components/FileViewer.tsx
      lines: 116-123
      excerpt: |
        html = html
          .replace(/`([^`]{1,200})`/g, '<code ...>$1</code>')
          .replace(/\*\*([^\n]{1,200})\*\*/g, "<strong>$1</strong>")
          .replace(/\*([^*\n]{1,200})\*/g, "<em>$1</em>")
          .replace(/^### (.+)$/gm, '<h3 ...>$1</h3>')
          ...
          .replace(/\n/g, "<br />");
        // NOTE: no [text](url) link transform here — but headings take (.+)
        // which matches all characters in the pre-escaped content.
    - reproduction: |
        renderMarkdownContent is used via dangerouslySetInnerHTML at line 311.
        Unlike Chat.tsx's renderMarkdownImpl (which at line 335-340 restricts hrefs
        to https?://), FileViewer.tsx renderMarkdownContent has NO link transform
        at all. This means no link XSS today, but the heading transforms
        (`^## (.+)$`) use (.+) which is unbounded across the already-HTML-escaped
        text. The risk is low given the current transforms, but the pattern is
        divergent from Chat.tsx and one added transform (e.g. links) without the
        http-only guard would introduce XSS.
        More concretely: both markdown renderers are hand-rolled. Any merge/copy
        of a feature from one to the other without copying the XSS guard is a silent
        regression path.
  blast_radius: |
    Medium. Currently FileViewer renders markdown from workspace file contents
    (files generated by the agent). If an agent creates a malicious .md file,
    a future link transform without the https-check would execute arbitrary JS in
    the app's origin. Content is served to the authenticated user only, so the
    attack path requires either a compromised agent or prompt injection.
  proposed_fix: |
    (a) Consolidate to a single shared renderMarkdown utility used by both
    FileViewer.tsx and Chat.tsx, so XSS mitigations are applied consistently.
    (b) Or at minimum, add an explicit comment in FileViewer.tsx's renderMarkdownContent
    noting the BUG-R2-14 XSS safety note already present in Chat.tsx, and add a
    lint rule / test that asserts renderMarkdownContent never produces <a href=
    without the https check.
  fix_type: frontend_patch
  test_to_add: |
    "fileviewer-markdown-xss": pass a markdown string with a link to a
    javascript: URI through renderMarkdownContent; assert the output does not
    contain href="javascript: (i.e., link is stripped or escaped).
  blocking: [none]
  confidence: medium

- id: A4-06
  severity: P2
  category: security
  surface: frontend
  title: PreviewPane iframe uses allow-same-origin — sandboxed user app shares the app's origin, enabling postMessage XSS
  evidence:
    - file: /home/user/workspace/mariana/frontend/src/components/deft/PreviewPane.tsx
      lines: 279-287
      excerpt: |
        <iframe
          key={iframeKey}
          src={absoluteUrl}
          title="Live preview"
          sandbox="allow-scripts allow-same-origin allow-forms allow-modals allow-popups allow-downloads"
          className={...}
        />
    - file: /home/user/workspace/mariana/frontend/src/lib/agentRunApi.ts
      lines: 127-132
      excerpt: |
        export function previewAbsoluteUrl(relUrl: string): string {
          const apiBase = (import.meta.env.VITE_API_URL ?? "").replace(/\/+$/, "");
          if (/^https?:\/\//i.test(relUrl)) return relUrl;
          return `${apiBase}${relUrl.startsWith("/") ? "" : "/"}${relUrl}`;
        }
    - reproduction: |
        The manifest URL is served from the same backend IP/domain (77.42.3.206:8080
        for the preview/* rewrite, same origin as the app). With allow-same-origin,
        JS inside the preview iframe can:
          window.parent.document.cookie  → read app cookies (httpOnly protects JWTs,
          but PostHog and any non-httpOnly cookies are exposed)
          window.parent.postMessage(...)  → message app frame with no origin check
          localStorage access from same origin → read/write app's localStorage
  blast_radius: |
    If the agent generates or is prompted to generate a preview app with malicious
    JS, that JS runs in an iframe that shares the app's origin. It can read
    localStorage (Supabase anon key cached), modify DOM of the parent, or exfiltrate
    cookies not marked httpOnly. This is the same origin as the main SPA, so the
    standard same-origin policy gives no protection here. The preview route should
    be served from a separate origin (e.g. preview.deft.computer) or the sandbox
    should not include allow-same-origin.
  proposed_fix: |
    Option A (preferred): Serve the preview on a separate subdomain
    (preview.deft.computer) and remove allow-same-origin from the sandbox. This
    is the industry-standard approach (Vercel preview, CodeSandbox).
    Option B (stopgap): Remove allow-same-origin. This breaks some preview apps
    that use localStorage, but eliminates the parent-frame access risk.
    Do not combine allow-scripts + allow-same-origin for user-generated content
    on the same origin — this is equivalent to no sandbox at all.
  fix_type: config
  test_to_add: |
    "preview-iframe-origin-isolation": assert that the iframe src hostname
    differs from window.location.hostname; or assert sandbox attribute
    does not contain both allow-scripts and allow-same-origin simultaneously.
  blocking: [none]
  confidence: high

- id: A4-07
  severity: P2
  category: security
  surface: frontend
  title: AuthContext loading spinner renders outside all providers — crashes if supabaseConfigError is falsy but env partially broken
  evidence:
    - file: /home/user/workspace/mariana/frontend/src/contexts/AuthContext.tsx
      lines: 270-276
      excerpt: |
        // BUG-015: Show a loading spinner instead of a blank screen while session loads
        if (loading) {
          return (
            <div className="flex h-screen items-center justify-center bg-background">
              <div className="h-5 w-5 animate-spin rounded-full border-2 border-border border-t-primary" />
            </div>
          );
        }
    - reproduction: |
        The loading spinner is rendered directly by AuthProvider, which is a
        Context Provider sitting INSIDE BrowserRouter (App.tsx line 161).
        This means the spinner bypasses the children prop entirely and renders
        bare — without TooltipProvider, Toaster, or any other provider from
        App.tsx wrapping it. If any provider above AuthProvider throws (e.g.
        QueryClientProvider before auth resolves), the ErrorBoundary catches it
        rather than the AuthProvider-level spinner.
        More importantly: onAuthStateChange fires with INITIAL_SESSION as the
        FIRST event, which is asynchronous. Until it fires, loading=true. If
        the Supabase WS connection is slow (cold start, network issue), the
        spinner shows for multiple seconds with NO timeout or fallback. Users on
        flaky connections may see the spinner indefinitely.
  blast_radius: |
    On slow networks (mobile 3G, corporate proxies), the app displays a full-screen
    spinner with no user feedback and no timeout. If onAuthStateChange never fires
    (Supabase service outage), the app is permanently stuck. Affects all users on
    initial load. Not a crash path, but a significant availability concern.
  proposed_fix: |
    Add a maximum loading timeout (e.g. 8–10 s) after which loading is forced
    to false and user is treated as unauthenticated with a dismissible error toast.
    This mirrors the pattern used for profile fetch retries (5 × 500 ms in syncSession).
    Also add a `data-testid="auth-loading"` attribute for test coverage.
  fix_type: frontend_patch
  test_to_add: |
    "auth-loading-timeout": mock onAuthStateChange to never fire; assert
    that after 10 s the loading spinner disappears and the user sees a
    recoverable error state rather than an infinite spinner.
  blocking: [none]
  confidence: medium

- id: A4-08
  severity: P2
  category: integrity
  surface: frontend
  title: success_url for checkout lands on pages that never call refreshUser — credit balance not updated after successful payment
  evidence:
    - file: /home/user/workspace/mariana/frontend/src/pages/Checkout.tsx
      lines: 85
      excerpt: |
        success_url: `${window.location.origin}/chat?checkout=success`,
    - file: /home/user/workspace/mariana/frontend/src/pages/Pricing.tsx
      lines: 210
      excerpt: |
        success_url: `${window.location.origin}/build?checkout=success`,
    - reproduction: |
        After a Stripe checkout, Stripe redirects to /chat?checkout=success or
        /build?checkout=success. Neither Chat.tsx nor Build.tsx reads the
        `checkout` query param or calls refreshUser()/refetchBalance on landing.
        The Stripe webhook fires asynchronously (usually 2-10 s after redirect),
        so credits may not yet be applied. The user lands with no UI feedback
        that checkout was successful, and sees their old credit balance.
        Account.tsx's success_url (/account?topup=success) also has no
        useSearchParams() read — confirmed by searching the file.
  blast_radius: |
    After any successful payment, the user sees their old credit balance until
    they manually reload or navigate to a page that triggers refreshUser().
    This creates a poor post-purchase experience and may cause users to attempt
    to purchase again. It also masks webhook delivery failures — if the webhook
    never fires, the user has no visible indication that their credits were not
    applied.
  proposed_fix: |
    On /chat, /build, and /account, check for the `?checkout=success` or
    `?topup=success` query param on mount. If present:
    (a) Show a success toast ("Payment received — credits updating…")
    (b) Poll refreshUser() or refetchBalance() with a short delay (e.g. 3 s)
        and up to 3 retries to wait for the webhook to apply credits
    (c) Clear the query param from the URL after handling it
    This pattern is established and safe: the param is set by the app's own
    origin (window.location.origin) so no open-redirect risk.
  fix_type: frontend_patch
  test_to_add: |
    "post-checkout-credit-refresh": navigate to /chat?checkout=success; assert
    refreshUser() is called and a success toast is displayed within 500 ms.
  blocking: [none]
  confidence: high

- id: A4-09
  severity: P3
  category: security
  surface: frontend
  title: Supabase JWT stored in localStorage by default — XSS window not mitigated by httpOnly flag
  evidence:
    - file: /home/user/workspace/mariana/frontend/src/lib/supabase.ts
      lines: 35
      excerpt: |
        _client = createClient(supabaseUrl, supabaseAnonKey);
        // No storage: option — defaults to localStorage
    - reproduction: |
        Supabase JS client v2 persists the session (access_token + refresh_token)
        in localStorage under the key `sb-{projectRef}-auth-token`.
        localStorage is readable by any JS on the same origin. If an XSS
        vulnerability is introduced (see A4-05, A4-06), the attacker can
        exfiltrate the refresh token, which is long-lived (60 days by default).
        This is a known trade-off documented by Supabase; it is NOT a Supabase bug,
        but is worth flagging given the app has hand-rolled markdown rendering and
        an allow-same-origin iframe (A4-06) on the same origin.
  blast_radius: |
    Any XSS on the same origin can steal both the access token (15 min) and
    the refresh token (60 days), allowing persistent session hijack. Low
    incremental risk relative to A4-04/A4-05/A4-06, but the combination of
    (a) no CSP, (b) hand-rolled markdown, (c) allow-same-origin preview iframe,
    and (d) JWT in localStorage creates a defense-in-depth failure at every layer.
  proposed_fix: |
    Short-term: Fix A4-04 (CSP) and A4-06 (iframe origin isolation) to reduce
    XSS surface. Medium-term: Consider passing `storage: { ... }` to createClient()
    with a sessionStorage backend (shorter persistence window) or a custom
    httpOnly-cookie wrapper via a thin server-side proxy. Note: sessionStorage is
    lost on tab close, which degrades UX. The right call depends on the threat model.
    At minimum, document the trade-off in a security decision record.
  fix_type: config
  test_to_add: |
    "jwt-storage-doc": not a code test — add a security ADR entry noting the
    localStorage trade-off and the mitigations required (CSP, iframe isolation).
  blocking: [A4-04, A4-06]
  confidence: high

- id: A4-10
  severity: P3
  category: availability
  surface: frontend
  title: vercel.json backend rewrite targets a bare IP over plain HTTP — MITM risk on API calls
  evidence:
    - file: /home/user/workspace/mariana/frontend/vercel.json
      lines: 5-10
      excerpt: |
        "rewrites": [
          { "source": "/api/:path*", "destination": "http://77.42.3.206:8080/api/:path*" },
          { "source": "/preview/:path*", "destination": "http://77.42.3.206:8080/preview/:path*" },
        ]
    - reproduction: |
        Traffic from Vercel edge → backend travels over plain HTTP to a bare IP.
        This means: (a) no TLS on the backend hop, (b) the destination is an IP
        rather than a hostname, preventing certificate pinning or SNI-based routing,
        (c) if Vercel's internal network is ever observed, all API tokens in request
        headers and response bodies are exposed in cleartext.
  blast_radius: |
    All authenticated API calls (Authorization: Bearer JWT) travel unencrypted
    on the Vercel → backend segment. On Vercel's infrastructure this is likely
    in a private network, but it is not guaranteed. Additionally, the plain HTTP
    destination means the backend cannot be fronted by a CDN or WAF without a
    separate TLS termination proxy. If the IP changes (infra migration), the
    rewrite silently breaks all API calls.
  proposed_fix: |
    (a) Assign a DNS hostname to the backend server and install a TLS certificate
    (Let's Encrypt or equivalent).
    (b) Update vercel.json to use https://api.deft.computer/api/:path* (or similar
    stable hostname).
    (c) This also enables future CDN fronting and WAF rules.
  fix_type: config
  test_to_add: |
    "backend-tls": integration test that asserts the rewrite destination URL
    starts with https:// and uses a hostname, not a bare IP.
  blocking: [none]
  confidence: high

- id: A4-11
  severity: P3
  category: security
  surface: frontend
  title: jsdom pinned at ^20.0.3 (dev dep) — known XSS bypass CVEs in 20.x; should be ≥24.x
  evidence:
    - file: /home/user/workspace/mariana/frontend/package.json
      lines: 85
      excerpt: |
        "jsdom": "^20.0.3",
    - reproduction: |
        jsdom 20.x has several known security issues patched in 21+ (e.g.,
        HTML parsing differences that allow CSS injection in test environments).
        While jsdom is a devDependency (used only by vitest), a vulnerable jsdom
        can affect test correctness: a malicious test fixture or a test that
        renders user-supplied content (e.g. from snapshots) could silently
        produce incorrect sanitization verdicts. jsdom ≥24.x contains significant
        security fixes. Current vitest ^3.2.4 supports jsdom ≥22.
  blast_radius: |
    Dev-environment only. No production impact. However, security tests that rely
    on jsdom's HTML parser to verify XSS sanitization may produce false-pass results
    if jsdom's parser differs from Chrome's. This is a testing infrastructure risk
    rather than a live vulnerability.
  proposed_fix: |
    Update devDependencies: "jsdom": "^24.0.0". Run `npm audit` after upgrading
    to confirm no new transitive vulnerabilities. Verify vitest tests still pass.
  fix_type: frontend_patch
  test_to_add: |
    "jsdom-version": CI check asserting resolved jsdom version is ≥24.
  blocking: [none]
  confidence: medium

- id: A4-12
  severity: P4
  category: ux
  surface: frontend
  title: AuthProvider loading spinner bypasses all error boundaries and toasters — user sees raw spinner with no accessibility role
  evidence:
    - file: /home/user/workspace/mariana/frontend/src/contexts/AuthContext.tsx
      lines: 270-276
      excerpt: |
        if (loading) {
          return (
            <div className="flex h-screen items-center justify-center bg-background">
              <div className="h-5 w-5 animate-spin rounded-full border-2 border-border border-t-primary" />
            </div>
          );
        }
    - reproduction: |
        The loading div has no role="status", no aria-label, no aria-live region.
        Screen readers announce nothing. Compare with App.tsx's RouteFallback
        (lines 80-90) which correctly uses role="status" aria-live="polite"
        aria-label="Loading page" and a sr-only text span.
  blast_radius: |
    Screen reader users see no feedback during auth initialization (which can take
    up to 2.5 s on first visit per the 5-retry profile fetch). Affects all users
    with assistive technology on first load.
  proposed_fix: |
    Update the loading div to match RouteFallback's accessibility pattern:
      <div role="status" aria-live="polite" aria-label="Loading"
           className="flex h-screen items-center justify-center bg-background">
        <div aria-hidden className="h-5 w-5 animate-spin ..." />
        <span className="sr-only">Loading</span>
      </div>
  fix_type: frontend_patch
  test_to_add: |
    "auth-loading-a11y": assert loading state renders an element with
    role="status" and aria-live="polite".
  blocking: [none]
  confidence: high
```

---

## Methodology Gaps

1. **No runtime network trace.** This audit was static analysis only. Several findings (A4-03, A4-10) would benefit from a live `curl` or Wireshark capture to confirm actual header values and TLS status on the backend hop. The live Supabase project was not queried for this lens.

2. **Chat.tsx SSE polling path not fully traced.** Chat.tsx is 3 901 lines. The fallback polling path (lines 1709+) was not traced end-to-end. There may be additional EventSource creation sites that also put the full JWT in the URL without a stream-token mint step.

3. **react-query cache invalidation on cross-tab logout not verified.** AuthContext dispatches `deft:logout` but the hook in useCredits.ts only listens for `deft:credits-changed`. Whether the credits cache is invalidated on logout was not confirmed end-to-end.

4. **No automated dependency audit (`npm audit`) was run.** Only jsdom version was manually reviewed. A full `npm audit --audit-level=moderate` against the lock file should be added to CI.

5. **Admin tab subtrees not read.** `pages/admin/tabs/*.tsx` (10 files) were not individually read. The admin shell was confirmed to have server-side verification on mount; individual tab mutation handlers were not audited for client-only role checks.

6. **Stripe session_id from success URL not examined.** Stripe can append `?session_id=cs_...` to the success URL. The frontend ignores this param entirely. This is generally safe (no server trust of session_id needed) but was not confirmed against the backend.

7. **PostHog `persistence: "localStorage+cookie"` (analytics.ts:44)** stores analytics identity in localStorage. If a Stripe session_id or user email ends up in a PostHog event property, it is persisted in localStorage. The analytics call sites were not audited for PII leakage.
