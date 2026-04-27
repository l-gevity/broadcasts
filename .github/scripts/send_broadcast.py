#!/usr/bin/env python3
"""Render and send a broadcast via Azure Communication Services Email.

All configuration comes from env vars set by the workflow:
  DRY_RUN, GRAPH_TOKEN, ACS_TOKEN, EXT_APP_ID,
  ACS_ENDPOINT, SENDER_ADDRESS, REPLY_TO_ADDRESS, UNSUBSCRIBE_URL,
  REPO_FULL_NAME, REF, FILES (newline-separated paths)

Unsubscribe model: the footer link points to the profile page
(`UNSUBSCRIBE_URL`, e.g. https://l-gevity.nl/profile.html#marketing). Opt-in
already requires login (the profile-page toggle is the only opt-in surface),
so requiring login to opt out is symmetric with consent under GDPR Recital 32.
The same URL is used for every recipient, which is what enables BCC batching
below.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import markdown as md
import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape
from jinja2 import TemplateNotFound

GRAPH_USERS_BATCH_SIZE = 999
# ACS Email standard-tier limit is 50 recipients (to+cc+bcc combined) per
# /emails:send request. We use only BCC, so the cap is 50 per batch.
BCC_BATCH_SIZE = 50
# ACS rate limits (custom-domain default): 30 emails/min, 100 emails/hour
# per subscription, counted by recipient. Pause between batches keeps a 50-BCC
# burst from front-loading the per-minute window. See DECISIONS.md §
# "Rate limits and pacing".
SEND_PAUSE_SECONDS = 0.5

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = REPO_ROOT / "templates"

# `body` and `unsubscribe_url` are the rendering context's reserved keys —
# frontmatter cannot override them.
RESERVED_TEMPLATE_KEYS = frozenset({"body", "unsubscribe_url"})

_jinja_env: Environment | None = None


def _get_jinja_env() -> Environment:
    """Lazy-init Jinja env so import doesn't fail before templates exist."""
    global _jinja_env
    if _jinja_env is None:
        if not TEMPLATES_DIR.is_dir():
            sys.exit(f"ERROR: templates directory not found at {TEMPLATES_DIR}")
        _jinja_env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=select_autoescape(["html"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
    return _jinja_env


def env(name: str, *, required: bool = True, default: str = "") -> str:
    val = os.environ.get(name, default)
    if required and not val:
        sys.exit(f"ERROR: required env var {name} is not set")
    return val


def parse_broadcast(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        sys.exit(f"ERROR: {path} has no YAML frontmatter (must start with '---')")
    parts = text.split("---", 2)
    if len(parts) < 3:
        sys.exit(f"ERROR: {path} frontmatter not terminated (need closing '---')")
    fm = yaml.safe_load(parts[1]) or {}
    body = parts[2].lstrip("\n")
    if not fm.get("subject"):
        sys.exit(f"ERROR: {path} frontmatter missing required key: subject")
    return fm, body


def rewrite_images(html: str, repo: str, ref: str) -> str:
    """Rewrite relative `images/foo.png` references to jsDelivr CDN URLs.

    Branch-pinned via the workflow's git ref so a sent email always loads the
    image as it was at send time, even if the file is later edited.
    """
    cdn_base = f"https://cdn.jsdelivr.net/gh/{repo}@{ref}/"
    return re.sub(
        r'(<img\s+[^>]*?src=")(images/[^"]+)(")',
        lambda m: m.group(1) + cdn_base + m.group(2) + m.group(3),
        html,
    )


def build_email_html(rendered_body: str, fm: dict, unsubscribe_url: str) -> str:
    template_name = (fm.get("template") or "default") + ".html"
    try:
        template = _get_jinja_env().get_template(template_name)
    except TemplateNotFound:
        sys.exit(
            f"ERROR: template '{template_name}' not found in {TEMPLATES_DIR}. "
            f"Either add it or omit `template` in the broadcast frontmatter."
        )
    # Frontmatter values fill the rest of the rendering context. Reserved keys
    # (body, unsubscribe_url) come from the renderer, not the author.
    context = {k: v for k, v in fm.items() if k not in RESERVED_TEMPLATE_KEYS}
    return template.render(
        body=rendered_body,
        unsubscribe_url=unsubscribe_url,
        **context,
    )


def build_plain_text(fm: dict, body_md: str, unsubscribe_url: str) -> str:
    return (
        f"{fm.get('subject', '')}\n\n"
        f"{body_md.strip()}\n\n"
        f"--\nJe ontvangt deze e-mail omdat je je hebt aangemeld voor updates van L-GEVITY.\n"
        f"Afmelden: {unsubscribe_url}\n"
    )


def fetch_recipients(graph_token: str, ext_app_id: str) -> list[dict]:
    """List all opted-in, enabled CIAM users with their oid + mail."""
    attr_name = f"extension_{ext_app_id}_marketingOptInAt"
    base = "https://graph.microsoft.com/v1.0/users"
    params = {
        "$select": f"id,mail,{attr_name}",
        "$filter": f"{attr_name} ne null and accountEnabled eq true",
        "$count": "true",
        "$top": str(GRAPH_USERS_BATCH_SIZE),
    }
    url = f"{base}?{urllib.parse.urlencode(params)}"
    headers = {
        "Authorization": f"Bearer {graph_token}",
        "ConsistencyLevel": "eventual",
    }
    recipients: list[dict] = []
    while url:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
        for u in data.get("value", []):
            if u.get("mail"):
                recipients.append({"oid": u["id"], "address": u["mail"]})
        url = data.get("@odata.nextLink")
    return recipients


def send_one(
    acs_token: str, acs_endpoint: str, payload: dict, *, max_retries: int = 5
) -> str:
    """POST to ACS /emails:send. Returns the operation ID. Retries on 429."""
    url = f"{acs_endpoint.rstrip('/')}/emails:send?api-version=2023-03-31"
    body = json.dumps(payload).encode()
    delay = 1.0
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {acs_token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req) as r:
                resp = json.loads(r.read())
                return resp.get("id", "(no operation id)")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries:
                retry_after = int(e.headers.get("Retry-After", "0") or 0)
                sleep_for = max(delay, float(retry_after))
                print(
                    f"  429 throttled, sleeping {sleep_for:.1f}s before retry "
                    f"{attempt}/{max_retries}"
                )
                time.sleep(sleep_for)
                delay *= 2
                continue
            raise RuntimeError(
                f"ACS send failed (HTTP {e.code}): {e.read().decode()[:300]}"
            ) from e
    raise RuntimeError("ACS send: exhausted retries")


def chunked(seq: list, size: int):
    """Yield successive `size`-sized chunks from `seq`."""
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def main() -> None:
    dry_run = env("DRY_RUN").lower() == "true"
    graph_token = env("GRAPH_TOKEN")
    acs_token = env("ACS_TOKEN")
    ext_app_id = env("EXT_APP_ID")
    acs_endpoint = env("ACS_ENDPOINT")
    sender_address = env("SENDER_ADDRESS")
    reply_to = env("REPLY_TO_ADDRESS")
    unsubscribe_url = env("UNSUBSCRIBE_URL")
    repo = env("REPO_FULL_NAME")
    ref = env("REF")
    files = [f.strip() for f in env("FILES").splitlines() if f.strip()]

    print(f"DRY_RUN: {dry_run}")
    print(f"Files to process: {files}")
    print(f"Unsubscribe URL: {unsubscribe_url}")

    print("Fetching recipients from Microsoft Graph...")
    recipients = fetch_recipients(graph_token, ext_app_id)
    print(f"Recipients (opted-in, enabled, with mail): {len(recipients)}")
    if not recipients:
        print("No recipients — nothing to send.")
        return

    md_renderer = md.Markdown(extensions=["extra", "sane_lists"])

    for path_str in files:
        path = Path(path_str)
        print(f"\n=== {path} ===")
        fm, body_md = parse_broadcast(path)
        rendered_body = md_renderer.reset().convert(body_md)
        rendered_body = rewrite_images(rendered_body, repo, ref)
        subject = fm["subject"]

        # Body is identical for every recipient — render once, send many.
        # This is what makes BCC batching legal: ACS rejects per-recipient
        # body variation in a single send call.
        html = build_email_html(rendered_body, fm, unsubscribe_url)
        text = build_plain_text(fm, body_md, unsubscribe_url)

        total = len(recipients)
        sent = 0
        for batch in chunked(recipients, BCC_BATCH_SIZE):
            # NB: ACS Email rejects most reserved headers (incl. List-Unsubscribe
            # / RFC 8058 one-click headers) with HTTP 400 "Request body validation
            # error. See property 'headers'". The footer <a> link in the email
            # body is the only unsubscribe path. Revisit if ACS adds first-class
            # one-click support.
            #
            # Recipients go in BCC so they don't see each other's addresses.
            # `to` is omitted: ACS allows BCC-only sends.
            payload = {
                "senderAddress": sender_address,
                "recipients": {
                    "bcc": [{"address": r["address"]} for r in batch],
                },
                "content": {"subject": subject, "html": html, "plainText": text},
                "replyTo": [{"address": reply_to}],
                "userEngagementTrackingDisabled": True,
            }

            sent += len(batch)
            if dry_run:
                sample = ", ".join(r["address"] for r in batch[:3])
                more = "" if len(batch) <= 3 else f" (+{len(batch) - 3} more)"
                print(
                    f"  [DRY] would send batch {sent - len(batch) + 1}-{sent}/{total}: "
                    f"{sample}{more}"
                )
                continue
            op_id = send_one(acs_token, acs_endpoint, payload)
            print(
                f"  sent batch {sent - len(batch) + 1}-{sent}/{total} "
                f"({len(batch)} recipients) -> operation {op_id}"
            )
            time.sleep(SEND_PAUSE_SECONDS)

        if dry_run:
            print("\n  [DRY] HTML preview (first 600 chars):")
            print("  " + html[:600].replace("\n", "\n  "))


if __name__ == "__main__":
    main()
