# l-gevity/broadcasts

Newsletter / broadcast content for L-GEVITY members.

## Contract

- **Public by design.** Anything committed here is indexed publicly _before_
  it is sent. Never include personally identifiable information, per-recipient
  content, or anything you wouldn't paste into a public Slack channel.
- **All recipients receive identical content.** No per-user templating beyond
  the unsubscribe footer. Every opted-in member sees exactly what is in the
  Markdown file.
- **One file = one broadcast.** A new file added to `broadcasts/*.md` on
  `main` triggers a send. Editing a file already on `main` does NOT
  retrigger.
- **Marketing only.** Mandatory service announcements (e.g. "we changed the
  login URL") use a different channel: in-app banner or transactional email
  from `noreply@l-gevity.nl`. Subscribers to this list have all opted in
  voluntarily and can opt out at any time.

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

## Templates

Email layouts live in `templates/*.html` and use
[Jinja2](https://jinja.palletsprojects.com/) syntax.

- `templates/base.html` — outer shell (head, viewport, table wrapper, footer
  slot). Defines empty `{% block header %}` and `{% block content %}` for
  child templates to override.
- `templates/default.html` — extends `base.html`, fills `content` with the
  rendered Markdown body. Used when broadcast frontmatter has no `template`
  key.
- `templates/partials/footer.html` — legal text + unsubscribe link, included
  by `base.html`.

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

## Workflow

`.github/workflows/send-broadcast.yml` runs on push to `main` for newly added
files in `broadcasts/`. Ships with `DRY_RUN: 'false'` (live). To temporarily
disable sends (e.g. while validating a new template), flip the env var via
PR review.

`.github/scripts/send_broadcast.py` does the rendering, recipient lookup,
and ACS REST send (BCC-batched, up to 50 recipients per call).

For the rationale behind every design choice (auth pattern, consent model,
templating engine, etc.), see [DECISIONS.md](./DECISIONS.md).

For the as-built infrastructure topology, see
[INFRASTRUCTURE.md](https://github.com/l-gevity/l-gevity/blob/develop/INFRASTRUCTURE.md)
in the main repo. For the original design discussion, see
[issue #333](https://github.com/l-gevity/l-gevity/issues/333).

## Unsubscribe

Every email's footer links to the L-GEVITY profile page (`UNSUBSCRIBE_URL`,
typically `https://l-gevity.nl/profile.html#marketing`). The recipient logs
in (if not already authenticated) and toggles off the same
`<l-gevity-marketing-opt-in>` switch they used to opt in. There is no
per-recipient token, no SWA `/api/unsubscribe` call in the new flow, and no
`USER_HMAC_KEY` shared between repos.

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
