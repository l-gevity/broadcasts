---
subject: 'L-GEVITY mailing list — BCC batching smoke test'
preheader: First live send under the new BCC-batched pipeline
from: L-GEVITY Broadcasts
---

# Hello!

This is the first live broadcast after the pipeline switched to BCC batching
with a profile-page unsubscribe link. If this email landed in your inbox, the
following has been validated end-to-end:

- Recipients are loaded into a single BCC array (up to 50 per
  `/emails:send` call) instead of one ACS call per address.
- The rendered body is identical for every recipient — no per-recipient
  HMAC token is baked into the footer.
- The "Afmelden" link below points to the profile page rather than the
  legacy `/api/unsubscribe?t=…` endpoint, so opting out flows through the
  same toggle that was used to opt in.
- ACS accepts a BCC-only payload (no `to` recipient) without rejecting
  the request.

If anything renders strangely or the unsubscribe link points somewhere
unexpected, reply to this message — that's exactly the feedback this test
is meant to surface.

Thanks for testing,  
**The L-GEVITY team**
