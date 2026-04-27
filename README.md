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
files in `broadcasts/`. Currently ships with `DRY_RUN: 'true'` hardcoded —
flip the env var via PR review to go live.

`.github/scripts/send_broadcast.py` does the rendering, recipient lookup,
HMAC unsubscribe URL generation, and ACS REST send.

See
[INFRASTRUCTURE.md](https://github.com/l-gevity/l-gevity/blob/develop/INFRASTRUCTURE.md)
in the main repo for the full architecture and
[issue #333](https://github.com/l-gevity/l-gevity/issues/333) for the design
rationale.

## Unsubscribe

Every email contains a one-click unsubscribe link in the footer (and the
RFC 8058 `List-Unsubscribe` header). Clicking it clears `marketingOptInAt`
for that recipient on the CIAM tenant and prevents future sends. The link is
HMAC-signed and stateless — no token table to maintain.
