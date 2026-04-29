# l-gevity/broadcasts

Newsletter / broadcast content for L-GEVITY members.

## Contract

- **Public by design.** Anything committed here is indexed publicly _before_
  it is sent. Never include personally identifiable information, per-recipient
  content, or anything you wouldn't paste into a public Slack channel.
- **All recipients receive identical content.** No per-user templating beyond
  the unsubscribe footer. Every recipient sees exactly what is in the
  Markdown file.
- **One file = one send.** A new file added to `broadcasts/*.md` on `main`
  triggers an automatic broadcast. Files in `service/*.md` are dispatched
  manually via the `dispatch-service.yml` workflow in this repo. Editing a
  file already on `main` does NOT retrigger anything.

### Two folders, two channels

| Folder | Purpose | Sender | Audience | Trigger |
| --- | --- | --- | --- | --- |
| `broadcasts/` | Marketing newsletters — opt-in only | `broadcasts@mail.l-gevity.nl` | Members with `marketingOptInAt` set | `send-broadcast.yml` on push (BCC-batched) |
| `service/` | Transactional service announcements (e.g. "we added a new feature") | `noreply@mail.l-gevity.nl` | All enabled members with email, under the existing service relationship | `dispatch-service.yml` — workflow_dispatch only, dry-run by default |

Service announcements MUST be genuine service-relationship communications,
not marketing dressed up as service. When in doubt, default to `broadcasts/`
and an opt-in CTA. See [DECISIONS.md](./DECISIONS.md) for the
distinguishing test.

## Authoring a broadcast

Create `broadcasts/YYYY-MM-DD-slug.md`:

```markdown
---
subject: Your subject line — visible in mail clients
preheader: One-line preview text shown next to the subject in inboxes
from: L-GEVITY Broadcasts
---

# Heading

Markdown body. Use standard markdown — headings, lists, links, blockquotes.

Embed images as `![alt](images/your-image.png)`. They are auto-rewritten to
the jsDelivr CDN at send time, so they render in mail clients without a
hosting dependency.
```

## Authoring a service announcement

Create `service/YYYY-MM-DD-slug.md` with the same frontmatter shape as a
broadcast, plus `kind: transactional`:

```markdown
---
subject: 'L-GEVITY: nieuwe nieuwsbrief — opt-in indien gewenst'
preheader: One-line preview shown next to the subject in inboxes
from: L-GEVITY <noreply@mail.l-gevity.nl>
kind: transactional
---

Markdown body. Same conventions as a broadcast — Markdown features, image
rewriting — but rendered and dispatched by `dispatch-service.yml`, not by
`send-broadcast.yml`. The path filter on `broadcasts/**/*.md` ensures
files under `service/` never trigger an auto-send.
```

To send, dispatch the workflow manually from your terminal:

```bash
# Pilot to first 5 recipients (dry-run preview comes free without --confirm)
gh workflow run dispatch-service.yml --repo l-gevity/broadcasts \
  -f file=service/2026-04-29-newsletter-announcement.md \
  -f confirm=true \
  -f limit=5

# Spot-check the pilot in your inbox, then send to everyone:
gh workflow run dispatch-service.yml --repo l-gevity/broadcasts \
  -f file=service/2026-04-29-newsletter-announcement.md \
  -f confirm=true
```

The dispatcher (`.github/scripts/dispatch_service.py`) fetches the file,
validates `kind: transactional` in frontmatter (safety guard), queries
Graph for all enabled members with email, and sends per-recipient (no BCC)
from `noreply@mail.l-gevity.nl`. Defaults are dry-run; you must pass
`-f confirm=true` to actually send.

## Templates

Email layouts live in `templates/*.html` and use
[Jinja2](https://jinja.palletsprojects.com/) syntax.

- `templates/base.html` — outer shell (head, viewport, table wrapper, footer
  slot). Defines empty `{% block header %}` and `{% block content %}` for
  child templates to override.
- `templates/default.html` — extends `base.html`, fills `content` with the
  rendered Markdown body. Used when broadcast frontmatter has no `template`
  key.
- `templates/partials/footer.html` — legal text included by `base.html`.
  Branches on the `kind` frontmatter key: marketing renders the
  unsubscribe link, transactional renders the service-mededeling note
  (no unsubscribe — there's nothing to unsubscribe from).

To use a different layout per broadcast, set `template` in frontmatter:

```yaml
---
template: announcement
subject: ...
---
```

If `template` is omitted, `default.html` is used. All other frontmatter keys
are passed to the template as variables — author once in YAML, reference as
`{{ key }}` in the template. `body` and `unsubscribe_url` are reserved keys
filled by the renderer; they cannot be overridden by frontmatter.

To add a layout, drop a new file in `templates/` (typically `{% extends
"base.html" %}`) and reference it by name (without the `.html`) in
frontmatter.

## Workflows

This repo has two send pipelines, each backed by a workflow + script pair:

- **Marketing**: `.github/workflows/send-broadcast.yml` runs on push to
  `main` for newly added files matching `broadcasts/**/*.md`. Ships with
  `DRY_RUN: 'false'` (live); to temporarily disable broadcast sends (e.g.
  while validating a new template), flip the env var via PR review.
  `.github/scripts/send_broadcast.py` does the rendering, recipient lookup
  (opt-in only), and ACS REST send (BCC-batched, up to 50 per call).
- **Transactional**: `.github/workflows/dispatch-service.yml` runs only on
  `workflow_dispatch` and is dry-run by default (`confirm: 'false'` input).
  `.github/scripts/dispatch_service.py` queries ALL enabled members with
  email, validates `kind: transactional` in the file frontmatter, and
  sends per-recipient.

Files under `service/` never trigger the marketing workflow (path filter
on `broadcasts/**/*.md`). Files under `broadcasts/` trip a safety guard
in the transactional dispatcher (rejected if `kind != 'transactional'`).

For the rationale behind every design choice (auth pattern, consent model,
templating engine, etc.), see [DECISIONS.md](./DECISIONS.md).

For the as-built infrastructure topology, see
[INFRASTRUCTURE.md](https://github.com/l-gevity/l-gevity/blob/develop/INFRASTRUCTURE.md)
in the main repo. For the original design discussion, see
[issue #333](https://github.com/l-gevity/l-gevity/issues/333).

## Unsubscribe

Marketing emails (`broadcasts/`) carry a footer link to the L-GEVITY
profile page (`UNSUBSCRIBE_URL`, typically
`https://l-gevity.nl/profile.html#marketing`). The recipient logs in (if
not already authenticated) and toggles off the same
`<l-gevity-marketing-opt-in>` switch they used to opt in. There is no
per-recipient token, no SWA `/api/unsubscribe` call in the new flow, and no
`USER_HMAC_KEY` shared between repos.

Transactional emails (`service/`) have no unsubscribe link — there is no
list to unsubscribe from. They are sent under the existing
service-relationship lawful basis (GDPR Art. 6(1)(b) / ePrivacy soft
opt-in for service announcements), one-shot per topic, and they explicitly
state in the body that they're a service announcement. If the recipient
does not want a related marketing follow-up, they simply don't opt in to
the marketing list.

This is symmetric with the consent surface: opt-in already requires login
(the profile-page toggle is the only opt-in path), so requiring login to opt
out satisfies GDPR Recital 32 ("withdrawal as easy as giving"). It also lets
the broadcast script send the **same body** to every recipient, which is
what unlocks ACS BCC batching — see
[DECISIONS.md § Profile-page unsubscribe](./DECISIONS.md#profile-page-unsubscribe-bcc-batching).

The RFC 8058 `List-Unsubscribe` header (the "one-click" button some inbox
providers show next to the sender name) is **not** sent: ACS Email's header
allowlist rejects it. Footer link only — see
[DECISIONS.md § No List-Unsubscribe headers](./DECISIONS.md#no-list-unsubscribe--rfc-8058-headers).
