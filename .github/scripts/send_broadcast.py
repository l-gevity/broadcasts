#!/usr/bin/env python3
"""Render and send a broadcast via Azure Communication Services Email.

All configuration comes from env vars set by the workflow:
  DRY_RUN, GRAPH_TOKEN, ACS_TOKEN, EXT_APP_ID, USER_HMAC_KEY,
  ACS_ENDPOINT, SENDER_ADDRESS, REPLY_TO_ADDRESS, UNSUBSCRIBE_BASE_URL,
  REPO_FULL_NAME, REF, FILES (newline-separated paths)

The HMAC scheme MUST match packages/api/unsubscribe/index.ts in
l-gevity/l-gevity:
  key = base64-decoded USER_HMAC_KEY
  hmac_input = "unsubscribe:" + oid
  signature = base64url(HMAC-SHA256(key, hmac_input)).rstrip("=")
  token = oid + "." + signature
"""

from __future__ import annotations

import base64
import hashlib
import hmac
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
SEND_PAUSE_SECONDS = 0.5  # gentle pacing under the 100/min ACS cap
HMAC_PURPOSE = "unsubscribe"

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


def hmac_unsubscribe_url(oid: str, key_b64: str, base_url: str) -> str:
    try:
        key = base64.b64decode(key_b64, validate=True)
    except (ValueError, base64.binascii.Error) as e:
        sys.exit(
            f"ERROR: USER_HMAC_KEY is not valid base64 ({e}). "
            "It must match the value used by the SWA /api/unsubscribe endpoint."
        )
    sig_bytes = hmac.new(
        key, f"{HMAC_PURPOSE}:{oid}".encode("utf-8"), hashlib.sha256
    ).digest()
    sig = base64.urlsafe_b64encode(sig_bytes).decode().rstrip("=")
    token = f"{oid}.{sig}"
    return f"{base_url}?t={urllib.parse.quote(token, safe='.')}"


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


def main() -> None:
    dry_run = env("DRY_RUN").lower() == "true"
    graph_token = env("GRAPH_TOKEN")
    acs_token = env("ACS_TOKEN")
    ext_app_id = env("EXT_APP_ID")
    user_hmac_key = env("USER_HMAC_KEY")
    acs_endpoint = env("ACS_ENDPOINT")
    sender_address = env("SENDER_ADDRESS")
    reply_to = env("REPLY_TO_ADDRESS")
    unsubscribe_base = env("UNSUBSCRIBE_BASE_URL")
    repo = env("REPO_FULL_NAME")
    ref = env("REF")
    files = [f.strip() for f in env("FILES").splitlines() if f.strip()]

    # Fail fast if USER_HMAC_KEY is malformed — easier to diagnose at startup
    # than per-recipient.
    hmac_unsubscribe_url("0" * 36, user_hmac_key, unsubscribe_base)

    print(f"DRY_RUN: {dry_run}")
    print(f"Files to process: {files}")

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

        last_html = ""
        for recipient in recipients:
            unsubscribe_url = hmac_unsubscribe_url(
                recipient["oid"], user_hmac_key, unsubscribe_base
            )
            html = build_email_html(rendered_body, fm, unsubscribe_url)
            text = build_plain_text(fm, body_md, unsubscribe_url)
            payload = {
                "senderAddress": sender_address,
                "recipients": {"to": [{"address": recipient["address"]}]},
                "content": {"subject": subject, "html": html, "plainText": text},
                "replyTo": [{"address": reply_to}],
                "userEngagementTrackingDisabled": True,
                "headers": [
                    {"name": "List-Unsubscribe", "value": f"<{unsubscribe_url}>"},
                    {
                        "name": "List-Unsubscribe-Post",
                        "value": "List-Unsubscribe=One-Click",
                    },
                ],
            }
            last_html = html

            if dry_run:
                print(
                    f"  [DRY] would send to {recipient['address']} "
                    f"(oid={recipient['oid'][:8]}...)"
                )
                continue
            op_id = send_one(acs_token, acs_endpoint, payload)
            print(f"  sent to {recipient['address']} -> operation {op_id}")
            time.sleep(SEND_PAUSE_SECONDS)

        if dry_run and last_html:
            print("\n  [DRY] HTML preview (first 600 chars):")
            print("  " + last_html[:600].replace("\n", "\n  "))


if __name__ == "__main__":
    main()
