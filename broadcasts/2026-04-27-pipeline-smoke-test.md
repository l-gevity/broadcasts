---
subject: 'L-GEVITY mailing list — pipeline smoke test'
preheader: First dry-run of the broadcasts pipeline (no email sent)
from: L-GEVITY Broadcasts
---

# Hello!

This is a smoke test of the L-GEVITY broadcasts pipeline.

If you're reading this in a GitHub Actions log (and not in your inbox), the
following has been validated end-to-end:

- The federated OIDC service principal `gh-broadcasts` can authenticate to
  Azure and acquire a token for ACS Email.
- The CIAM service principal `gh-broadcasts-graph` can authenticate via
  client credentials and query Microsoft Graph.
- The Graph filter
  `extension_<extAppId>_marketingOptInAt ne null and accountEnabled eq true`
  resolves to at least one opted-in member.
- The Markdown → HTML rendering produces a sensible email shell via the
  Jinja2 `default.html` template.
- Per-recipient HMAC unsubscribe URLs match the SWA `/api/unsubscribe`
  verifier scheme.

The workflow is currently in **dry-run mode** (`DRY_RUN: 'true'` in
`.github/workflows/send-broadcast.yml`). No actual email leaves the
infrastructure. To go live: open a PR flipping `DRY_RUN` to `'false'`,
review, merge, then commit a real broadcast.

Thanks for testing,  
**The L-GEVITY team**
