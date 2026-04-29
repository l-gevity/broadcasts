# Design decisions

The "why" behind how this repo, the workflow, and the surrounding
infrastructure are shaped. Each entry captures a choice made, the
reason, the alternatives ruled out, and (where relevant) what would
trigger re-examining the choice.

For the as-built infrastructure topology (Azure resources, DNS records,
service principals, secrets), see
[INFRASTRUCTURE.md](https://github.com/l-gevity/l-gevity/blob/develop/INFRASTRUCTURE.md)
in the main repo. For the original design discussion, see
[issue #333](https://github.com/l-gevity/l-gevity/issues/333).

---

## Table of contents

1. [Architecture & scope](#architecture--scope)
2. [Consent model & GDPR](#consent-model--gdpr)
3. [Identity & permissions](#identity--permissions)
4. [Sender domain & deliverability](#sender-domain--deliverability)
5. [Templates & rendering](#templates--rendering)
6. [Sending mechanics](#sending-mechanics)
7. [Operations & safety](#operations--safety)

---

## Architecture & scope

### Standalone repo, not in the main monorepo

`l-gevity/broadcasts` is a separate repository, not a folder in
`l-gevity/l-gevity`. Every commit to the main monorepo triggers SWA
build/deploy pipelines for the production and acceptance sites — those
deploys are minutes long and consume Azure capacity. A broadcast commit
has no reason to retrigger the entire site build, and a site build has
no reason to wake up the broadcasts workflow. Isolating the repos
removes the cross-talk.

The trade-off is that some context lives in two places (this repo plus
`INFRASTRUCTURE.md` in the main repo). Cross-references handle that.

### Public-by-design repository

This repo is public. Every Markdown file is indexed publicly the moment
it lands on `main` — before any email goes out. The contract is
explicit: never commit personally identifiable information,
per-recipient content, or anything that wouldn't be appropriate as a
public Slack message.

The benefit is that the repo *is* the audit trail. Anyone (including
the recipient) can verify the exact wording sent, the time it was sent,
and the rendered template. No separate "what did we send" archive is
needed.

The constraint shapes downstream choices: there is no per-recipient
templating, no merge-tags ("Hello $first_name"), and no segmented
content. All opted-in members get exactly what is in the Markdown file.

### One file = one broadcast; newly-added files only

The workflow detects newly added Markdown files via
`git diff --diff-filter=A --name-only $BEFORE $HEAD -- 'broadcasts/**/*.md'`.
Editing a file that has already been merged on `main` does NOT
retrigger a send. Removing a file does nothing. This is deliberate:
broadcasts are immutable once sent. If a correction is needed, it goes
out as a new broadcast.

The first push to `main` is an edge case (the `before` SHA is all
zeros) — the workflow treats every broadcast file present at that
moment as new. In practice this only happens once at repo
initialization.

`workflow_dispatch` with a `file` input bypasses the diff detection so
testing flows can re-trigger an already-sent file deliberately.

### Two channels — `broadcasts/` (marketing) and `service/` (transactional)

This repo hosts BOTH channels with strictly different governance. The
folder a file lives in determines the entire send model:

- **`broadcasts/`** — opt-in marketing. Sender
  `broadcasts@mail.l-gevity.nl`. Audience: members with
  `marketingOptInAt` set. BCC-batched. Auto-triggered by
  `send-broadcast.yml` on push.
- **`service/`** — transactional service announcements (feature changes,
  ToS updates, security notices, billing changes). Sender
  `noreply@mail.l-gevity.nl` (separate ACS sender username on the same
  verified subdomain, provisioned 2026-04-29). Audience: all enabled
  members with email, regardless of marketing consent. Per-recipient
  (no BCC). Manually dispatched via `dispatch-service.yml`
  (`workflow_dispatch` only, dry-run by default).

The legal basis differs by folder, not by content:

- `broadcasts/` → GDPR Art. 6(1)(a) explicit consent (the
  `marketingOptInAt` extension records when consent was given).
- `service/` → GDPR Art. 6(1)(b) contract performance / ePrivacy
  soft-opt-in for service announcements (the recipient has an active
  L-GEVITY account; the message concerns their service, not promotion).

A file MUST declare `kind: transactional` in its frontmatter to be
sent via `dispatch-service.yml`. The dispatcher rejects files without
this guard. This prevents an accidentally-misrouted marketing file
from reaching all members under a "transactional" framing.

Use `service/` only for genuine service-relationship communications.
The distinguishing test: would a member be surprised or upset to
receive this if they had explicitly declined marketing? If yes, it's
marketing — put it in `broadcasts/` with an opt-in CTA.

The previous formulation of this section ("transactional email from
`noreply@l-gevity.nl`") was aspirational; the apex domain is not yet
verified in ACS, so the transactional sender currently lives on the
same verified subdomain as broadcasts (`mail.l-gevity.nl`) with a
distinct local-part. Verifying the apex would let us separate
reputation pools further.

### No bounce/complaint handling at current scale

ACS Email exposes bounce and complaint events via Event Grid. We don't
consume them yet. At ~74 members, manual feedback (a member tells us
their address bounced) is sufficient, and the deliverability cost of
not auto-suppressing bouncing addresses is bounded.

**Revisit if** total cadence exceeds weekly digests or the member base
crosses ~500. Both increase the risk of cumulative bounce damage to
sender reputation.

---

## Consent model & GDPR

### Single attribute `marketingOptInAt` (timestamp + presence as audit trail)

The original draft proposed two custom attributes on the CIAM user:
`marketingOptIn` (Boolean) and `marketingOptInAt` (timestamp). We
collapsed those into one nullable `String` attribute. The presence of
an ISO 8601 timestamp means opted-in (and *when* they opted in is the
audit trail); `null` means opted-out (or never opted in). The Graph
filter `marketingOptInAt ne null and accountEnabled eq true` is the
recipient query.

This halves the attribute surface, removes the possibility of two
fields disagreeing (boolean = true but timestamp = null, or vice
versa), and gives the GDPR audit-trail value for free.

### Opt-in via profile page only — no sign-up checkbox

The original design called for a checkbox on the CIAM sign-up flow
that, when ticked, wrote `marketingOptInAt = <now>`. Implementing this
on Entra External ID requires either a custom Authentication Extension
(a new SWA Function in the sign-up critical path with its own auth
surface and failure mode) or a custom branded HTML sign-up page. Both
are heavier than the expected lift in opt-in rate justifies at current
scale.

Instead, opt-in is driven entirely by the
`<l-gevity-marketing-opt-in>` toggle on the `/profile` page. The
trade-off is a lower opt-in rate than a sign-up checkbox would yield,
but the simpler critical path and zero CIAM-portal manual configuration
outweigh that at 74 members.

**Revisit if** opt-in rate via the profile toggle proves too low to
justify maintaining the broadcast pipeline.

### Server-stamps the timestamp; never trusts client

The `PUT /api/profile/marketing-opt-in` endpoint accepts only a Boolean
(`{"optedIn": true}` or `{"optedIn": false}`). The server generates the
ISO 8601 timestamp on opt-in and writes `null` on opt-out. The client
cannot supply a timestamp.

This prevents a malicious or buggy client from claiming consent at a
fake date — the audit trail is trustworthy. It also keeps the API
shape simple (one Boolean in, current state out).

### Engagement tracking disabled

ACS Email supports per-recipient open/click tracking — every link is
rewritten to a tracking redirector, every email embeds a tracking
pixel. We disable this (`userEngagementTrackingDisabled: true`).

If enabled, the tracking creates a flow of personal data
(IP-correlated open/click events) that would need to be disclosed in
the privacy policy and added to the data inventory. Disabling keeps
the privacy story clean: we send identical content to consenting
members and have no idea who opened what.

**Revisit if** content effectiveness becomes a bottleneck and the
GDPR overhead of disclosing tracking is worth the engagement data.

### Profile-page unsubscribe (BCC batching)

The footer link points to the L-GEVITY profile page
(`UNSUBSCRIBE_URL`, e.g. `https://l-gevity.nl/profile.html#marketing`).
Clicking it lands the recipient on the same `<l-gevity-marketing-opt-in>`
toggle they used to opt in. They authenticate (if not already) and flip it
off; the existing `PUT /api/profile/marketing-opt-in` endpoint clears
`marketingOptInAt`. There is no per-recipient token, no
`/api/unsubscribe` call in the new flow, and no `USER_HMAC_KEY` shared
between this repo and the main monorepo.

The chosen design has three properties that compound:

1. **Symmetric with consent under GDPR Recital 32.** Opt-in is gated
   behind login (the profile-page toggle is the only opt-in surface — see
   "Opt-in via profile page only" above). Requiring login to opt out is
   "as easy as giving consent", since giving consent itself requires
   login. The asymmetry that GDPR guards against — anonymous one-click
   opt-in followed by login-gated opt-out — is structurally absent here.

2. **Same body for every recipient.** The original HMAC scheme baked a
   per-recipient signature into the `<a href>` in the footer, which made
   the rendered HTML different for every recipient and forced one ACS
   call per recipient. With a constant URL the body is identical across
   the cohort, which lets us put up to 50 recipients in a single
   `recipients.bcc` array — ACS Email's standard-tier per-call cap. That
   is the structural win that motivated the redesign; see "BCC-batched
   send" below.

3. **No shared HMAC key, no SWA endpoint to maintain in two places.**
   The previous model required `USER_HMAC_KEY` to be present in both
   `l-gevity/broadcasts` (to mint tokens) and `l-gevity/l-gevity` (to
   verify them). Rotating it broke every outstanding unsubscribe link
   in flight. The new model removes this coupling: broadcasts don't
   need any cryptographic material, and the SWA `/api/unsubscribe`
   endpoint becomes legacy (kept only to honor old emails sent before
   the redesign — see "Legacy `/api/unsubscribe` endpoint preserved").

The trade-off is one extra step for the recipient (login on the profile
page) versus the previous one-click-from-email behavior. At our scale
and given the symmetric-consent model, this is acceptable.

**Revisit if** the profile-page hop produces a complaint or a
deliverability signal (e.g. spam-folder reports rise because users
report-as-spam instead of clicking through to opt out).

### Legacy `/api/unsubscribe` endpoint preserved

Emails sent before the redesign carry HMAC-signed
`https://l-gevity.nl/api/unsubscribe?t=<oid>.<sig>` URLs in their
footers. Those emails live in member inboxes indefinitely. Deleting the
endpoint would break them. The endpoint is therefore retained as-is —
it still validates the HMAC, clears `marketingOptInAt`, and shows the
confirmation page — and is marked deprecated in code comments. New
broadcasts do not generate these URLs and the workflow no longer needs
`USER_HMAC_KEY`.

### Consent state read fresh from Graph, not from the ID token

`marketingOptInAt` is deliberately NOT in the optional claims of the ID
token, even though the other extension attributes
(`tosAcceptedVersion`, `redeemedPromoCode`, `screenName`) are. The
profile toggle clears or sets the attribute mid-session, and the
legacy `/api/unsubscribe` endpoint can clear it without a login at
all, so a session token issued before either of those would carry
stale opt-in state for up to an hour.

The consequence is one extra Graph call per profile-page render to
read the current state. At low traffic this is negligible.

---

## Identity & permissions

### Federated OIDC for ACS access; client secret for Graph (Option B)

The broadcasts workflow needs two distinct credentials:
- **Azure plane** (ACS Email REST): handled by federated OIDC. App
  registration `gh-broadcasts` in the L-GEVITY tenant has a federated
  credential pinned to `repo:l-gevity/broadcasts:ref:refs/heads/main`,
  granted the `Communication and Email Service Owner` role on the
  `acs-androman-nl` resource only. No static secret.
- **Microsoft Graph** (CIAM tenant, recipient lookup): handled by
  client credentials. App registration `gh-broadcasts-graph` in the
  CIAM tenant has a 2-year client secret stored as
  `CIAM_CLIENT_SECRET` in this repo's secrets, with `User.Read.All`
  application permission (admin-consented).

We considered three patterns:

- **Option A**: Federated OIDC in both tenants — zero static secrets.
  Cleanest from a credential-management standpoint but federating to
  the CIAM tenant is a new pattern (the main monorepo uses a static
  secret for CIAM Graph access too).
- **Option B (chosen)**: Federated for ACS, static secret for CIAM
  Graph. Matches the existing main-repo pattern, easy to operate and
  rotate.
- **Option C**: Reuse the main repo's `ENTRA_EXTERNAL_CLIENT_SECRET`
  (which has `User.ReadWrite.All`). Strict no — sharing credentials
  between repos collapses the blast radius. If broadcasts is
  compromised, the attacker would have write access to *all* CIAM
  users, not just read.

**Revisit if** rotating the CIAM client secret becomes painful, or if
Option A's pattern gets adopted elsewhere in the org.

### CIAM SP gets `User.Read.All` only — never `User.ReadWrite.All`

The unsubscribe link clears `marketingOptInAt` via Graph, but that
write happens server-side in the SWA `/api/unsubscribe` endpoint using
the existing main-repo CIAM credentials, not from this workflow. The
broadcasts workflow only needs to *list* opted-in users, so it gets
read-only.

If the broadcasts repo were compromised, the attacker can read CIAM
user emails — bad — but cannot mutate them. Strictly bounded blast
radius.

### `EXT_APP_ID` as a static repo variable, not resolved at runtime

The earlier draft of the workflow queried `/applications` on Graph at
runtime to find the `b2c-extensions-app` (the CIAM directory-extension
app whose ID prefixes every extension attribute name). That query
requires `Application.Read.All`, which is broader than this workflow
needs.

Instead, the appId (with hyphens stripped) is stored as the
`EXT_APP_ID` repo variable. The CIAM SP keeps `User.Read.All` only.
The directory-extension app is recreated rarely enough (effectively
never, in the absence of a CIAM-tenant migration) that maintaining
this as a variable is fine.

**Revisit if** the b2c-extensions-app is ever recreated. The variable
needs to be updated to the new appId.

### ACS RBAC scoped to the single resource, not subscription-wide

`Communication and Email Service Owner` is granted on
`acs-androman-nl` specifically, not on `rg-androman-nl` or the
subscription. If the SP credential is exposed, the attacker can manage
that one ACS resource and no other Azure resources.

The role is broader than ideal — it includes delete permission on the
ACS resource. There is no built-in "send only" role. A custom role
with just `Microsoft.Communication/communicationServices/email/send/action`
would be tighter; we accepted the built-in for simplicity. Resource
locks can mitigate accidental delete if needed.

---

## Sender domain & deliverability

### Customer-managed subdomain `mail.l-gevity.nl`, not Azure-managed

ACS Email offers two domain modes:
- **Azure-managed**: a subdomain of `azurecomm.net` provisioned
  instantly. Capped at 5 emails/min and 10 emails/hour (no higher
  limits available). For testing only.
- **Customer-managed (chosen)**: a subdomain you own, with verified
  SPF + DKIM + DMARC DNS records. Initial default rate limits apply
  (see "Rate limits and BCC batching" below). Suitable for production.

We use `mail.l-gevity.nl` rather than the apex `l-gevity.nl` because
sending from a dedicated subdomain isolates broadcast reputation from
the apex domain. If a broadcast triggers a spam complaint cascade,
only `mail.l-gevity.nl` reputation is affected; transactional mail
from a future `noreply@l-gevity.nl` would be unaffected.

### `dataLocation: europe` for both ACS and ECS

Azure Communication Services lets you pick where message metadata is
stored at rest. We chose `europe` so message metadata never leaves the
EU, simplifying the GDPR data inventory.

### DMARC starts at `p=none`, tightens later

The DMARC record at `_dmarc.mail.l-gevity.nl` ships with
`v=DMARC1; p=none; rua=mailto:info@l-gevity.nl`. `p=none` means
receiving mail servers report on alignment failures but don't act on
them. After ~2 weeks of clean aggregate reports, tighten to
`p=quarantine` (failing mail goes to spam). After another 2-4 weeks,
tighten to `p=reject` (failing mail is rejected outright).

The phased approach catches misconfiguration (e.g. an SPF record that
forgot to include a relayer we use) before it starts blocking mail.

### DKIM CNAMEs must stay DNS-only on Cloudflare

`selector1-azurecomm-prod-net._domainkey.mail.l-gevity.nl` and the
`selector2-...` CNAME both point to `azurecomm.net`. They MUST be
created with the Cloudflare proxy disabled (DNS-only, grey cloud).
Cloudflare's HTTP proxy doesn't proxy CNAMEs cleanly; if proxy is on,
DKIM key resolution breaks and signatures fail to verify on receiving
servers.

`INFRASTRUCTURE.md` records this as a constraint in the DNS Records
table.

---

## Templates & rendering

### Jinja2 templating engine

Jinja2 was chosen over three alternatives:

- **Simple regex `{{var}}` substitution** — works for trivial cases
  but can't do conditionals, loops, partials, or template inheritance.
  Migrating away later means rewriting every template.
- **Jinja2 (chosen)** — pure-Python, ~50KB dependency, supports
  inheritance, conditionals, loops, macros, includes. Industry
  standard.
- **MJML / Maizzle** — email-specific markup that compiles to
  bulletproof table-based HTML. Adds Node.js to the toolchain (the
  workflow currently only needs Python). Worth adopting only if
  Outlook rendering bugs appear that the table-based shell can't
  handle.

We chose Jinja early — even though the first template is a trivial
`{% extends "base.html" %}{% block content %}{{ body|safe }}{% endblock %}`
— because the migration cost from regex to Jinja later would touch
every template, and Jinja's marginal cost now is one `pip install`
line plus a few lines of Python.

**Revisit if** Outlook rendering shows breakage that warrants MJML.

### Template inheritance with `base.html` + `{% block %}` slots

`templates/base.html` defines the HTML shell (head, viewport,
table-based wrapper, footer slot) and exposes empty `{% block header %}`
and `{% block content %}` blocks. Variant templates extend it:

```jinja
{% extends "base.html" %}
{% block header %}<tr><td>...</td></tr>{% endblock %}
{% block content %}{{ body | safe }}{% endblock %}
```

This means a future "newsletter with hero image and CTA button"
template only writes the unique pieces, not the entire shell again.
If the footer ever changes, it changes once in
`templates/partials/footer.html` and propagates everywhere.

### `body` and `unsubscribe_url` are reserved frontmatter keys

The renderer fills `body` and `unsubscribe_url` from the rendered
Markdown and the per-recipient HMAC. Frontmatter cannot override
either — the script filters them out before passing the rest of the
frontmatter to the template.

This means an author can't accidentally (or maliciously) write
`unsubscribe_url: https://attacker.com` in frontmatter and have it
applied to all recipients.

### Auto-escape on, body marked `| safe`

Jinja's `select_autoescape(["html"])` means any frontmatter value
inserted via `{{ var }}` is HTML-escaped automatically. This protects
against an author putting `<script>` in a subject or preheader.

The Markdown-rendered body is HTML and would be double-escaped without
opt-out, so it's marked `{{ body | safe }}` in the template. The
trade-off: anyone who can commit Markdown can write arbitrary HTML
(via Markdown's "extra" extension which preserves raw HTML). Authors
of this repo are trusted, and the public-by-design rule means any
malicious Markdown is publicly visible before it sends.

### Image references via jsDelivr CDN, branch-pinned

A Markdown reference like `![alt](images/foo.png)` is rewritten at
send time to
`https://cdn.jsdelivr.net/gh/l-gevity/broadcasts@<ref>/images/foo.png`.
jsDelivr is a free, fast, globally-cached CDN that serves directly
from public GitHub repos. We pin to the workflow's git ref (typically
the SHA of the broadcast commit), so a broadcast's images always look
the way they did at send time, even if the file is later edited or
the branch advances.

We considered hosting images in Azure Blob Storage with a CDN in front
— that would add a resource, an upload step, and a per-image URL
management surface. jsDelivr eliminates all of that for free.

**Revisit if** jsDelivr ever rate-limits us, or if a future broadcast
needs auth-gated images (which would require a real CDN).

---

## Sending mechanics

### BCC-batched send

The footer URL is a constant (the profile page) — every recipient sees
the same body. That makes ACS BCC batching legal: we put up to 50
recipients in `recipients.bcc` per `/emails:send` call. 50 is a hard
cap from
[the documented size limits for email](https://learn.microsoft.com/en-us/azure/communication-services/concepts/service-limits#size-limits-for-email)
("Number of recipients in email: 50", combined across to/cc/bcc); going
higher requires an Azure Support request to lift it. `to` is omitted;
ACS allows BCC-only sends and recipients don't see each other's
addresses.

The previous design sent one ACS call per recipient because the
footer link contained a per-recipient HMAC token, forcing per-recipient
body. That coupling is gone — see "Profile-page unsubscribe (BCC
batching)" above.

### Rate limits and pacing

ACS Email enforces two per-subscription rate limits on custom domains
(per
[the documented send-email rate limits](https://learn.microsoft.com/en-us/azure/communication-services/concepts/service-limits#rate-limits-for-email)):

| Window | Default cap | Higher limits available |
| --- | --- | --- |
| 1 minute | 30 emails | Yes (via support request) |
| 60 minutes | 100 emails | Yes (via support request) |

The cap counts **recipients**, not API calls. A BCC batch of 50 burns
50 of the per-minute and per-hour budget. The 60-minute cap is the one
that bites first on a multi-hundred-member broadcast: at 74 members in
a single send the 100/hour cap is the binding constraint until a
quota increase is requested.

We pause `SEND_PAUSE_SECONDS` (0.5s) between batches to avoid
back-to-back 30/min bursts and let ACS's internal pacer smooth the
flow. With 1 recipient per send the pacing is irrelevant; with 50 it
keeps us comfortably under 30/min.

**Revisit if** the broadcast cadence or membership grows past the
default 100/hour. Quota increases are documented in
[Quota increase for email domains](https://learn.microsoft.com/en-us/azure/communication-services/concepts/email/email-quota-increase)
and require a custom domain (which we have) plus a sub-1% failure
rate.

The earlier draft of this section quoted "~100 emails/minute". That
was wrong: 100 is the per-hour cap, not per-minute. The error was
caught while sizing the BCC batching change.

### Exponential backoff on HTTP 429 with `Retry-After` honored

If ACS throttles us with 429, the script sleeps for whichever is
larger: the `Retry-After` header value or an exponentially-doubling
local delay (1s, 2s, 4s, ...). Up to 5 retries per recipient. After
exhaustion, the script raises and the workflow fails — better to fail
loudly than to silently lose recipients.

### No `List-Unsubscribe` / RFC 8058 headers

The first attempt at sending added `List-Unsubscribe` and
`List-Unsubscribe-Post: List-Unsubscribe=One-Click` headers — the RFC
8058 mechanism that lets Gmail / Outlook show a one-click unsubscribe
button next to the sender name. ACS Email rejected the request with
HTTP 400 `Request body validation error. See property 'headers'`. ACS
maintains a header allowlist that excludes these.

The footer `<a>` link is the only unsubscribe surface for now. This
is functionally complete for opt-out compliance but means the
inbox-level one-click button isn't available.

**Revisit if** ACS adds first-class `List-Unsubscribe` support, or if
inbox providers start penalizing senders that lack it.

---

## Operations & safety

### `DRY_RUN` as a workflow-file env var, flipped via PR

The `DRY_RUN` flag lives in `.github/workflows/send-broadcast.yml` as
`env: DRY_RUN: 'false'`. It is NOT a `workflow_dispatch` input
because anyone with `Actions: write` could trigger a real send via
the dispatch UI without review. As an env var in the workflow file,
flipping it requires a PR + review + merge.

When `DRY_RUN` is `true`, the script logs `[DRY] would send to ...`
per recipient and prints an HTML preview, but doesn't call ACS. Use
this to validate template changes before going live.

### Branch detection bootstraps cleanly on first push

GitHub's `push` event provides a `before` SHA representing the
parent of the pushed commit. On the very first push to `main`, this
is forty zeros. The workflow detects that and treats every broadcast
file present at that moment as new (so a repo seeded with several
existing broadcasts would send all of them on first push).

In practice, the test broadcast was the first file in `broadcasts/`,
and the workflow had `DRY_RUN=true` at that moment, so no surprise
sends. The detection logic was exercised under the safest possible
conditions.

### `workflow_dispatch` accepts a `file` input for manual re-runs

`gh workflow run "Send Broadcast" -f file=broadcasts/2026-04-27-test.md`
runs the workflow against a specific file, bypassing the
newly-added-files filter. This is useful for:

- Re-running an already-sent broadcast against a single tester
  (after temporarily filtering the recipient list to one address).
- Testing template changes without committing a new broadcast file.
- Emergency re-send if the original delivery had infrastructure
  issues (note: this would also re-send to all recipients who didn't
  unsubscribe in the meantime — use with care).

### Credential rotation cadence

- `gh-broadcasts` (federated): no rotation needed (OIDC tokens are
  ephemeral).
- `gh-broadcasts-graph` (CIAM, client secret): 2-year expiry. Rotate
  via `az ad app credential reset --id <appId>`, then update
  `CIAM_CLIENT_SECRET` repo secret.
- `USER_HMAC_KEY`: not used by this repo any more (the unsubscribe URL
  is the profile-page link, no signing required). The key still lives
  on the SWA so the legacy `/api/unsubscribe` endpoint can verify
  tokens in old emails — see "Legacy `/api/unsubscribe` endpoint
  preserved" above. Rotation there breaks any old footer link still
  out in member inboxes; rotate only on suspected compromise.
