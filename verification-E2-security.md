# Verification E2 Security Review
2026-04-15

## Result: FAIL — 1 new vulnerability

## Vulnerabilities Found
### SEC-E2-01: Assistant message markdown link rendering allows DOM XSS via attribute injection
- File: /home/user/workspace/mariana/frontend/src/pages/Chat.tsx
- Lines: 195-230, 1749-1753  
- Severity: High
- Attack vector: The chat UI renders assistant/system message content with `dangerouslySetInnerHTML` after converting markdown links using a regex replacement that inserts the captured URL directly into an `<a href="$2">` HTML string without escaping quotes. A malicious payload such as `[click](https://example.com" onclick="alert(document.domain)")` produces an anchor with an injected event handler. Because assistant/system messages are rendered through this path, an attacker can deliver the payload by getting malicious text into model output (for example through prompt-injected web content, uploaded content reflected by the model, or any other untrusted external text the assistant repeats). When the victim clicks the rendered link, arbitrary JavaScript executes in the Mariana origin.
- Fix: Stop building HTML with regex string substitution for links. Either render markdown links as React elements, or sanitize generated HTML with a robust allowlist sanitizer before passing it to `dangerouslySetInnerHTML`. At minimum, HTML-escape quotes in captured attributes and reject any markdown link whose URL contains characters unsafe for an HTML attribute context.
