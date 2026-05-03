#!/usr/bin/env python3
"""Dispatch a transactional service announcement via ACS Email.

Sends per-recipient (no BCC) to all enabled CIAM members with email — used
for genuine service-relationship messages (feature changes, ToS updates,
security notices) that don't require marketing opt-in.

Workflow-dispatched only. See dispatch-service.yml and README.md.

Required env (set by dispatch-service.yml):
  DRY_RUN, GRAPH_TOKEN, ACS_TOKEN, EXT_APP_ID, ACS_ENDPOINT,
  SENDER_ADDRESS, SENDER_DISPLAY_NAME, REPLY_TO_ADDRESS,
  REPO_FULL_NAME, REF, FILE, LIMIT (optional), RECIPIENTS (optional)

When RECIPIENTS is set (newline- or comma-separated email list), the
Graph member query is bypassed and only those addresses receive the
send. Use for warm-up sends, beta cohorts, and tightly-scoped pilots
where the dispatcher's default of "all enabled members" is too broad.

Frontmatter contract:
  subject:    required
  preheader:  optional
  kind:       required, MUST equal 'transactional' (safety guard)
  template:   optional, defaults to 'default'
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import markdown as md

sys.path.insert(0, str(Path(__file__).resolve().parent))
from send_broadcast import (  # noqa: E402
    build_email_html,
    env,
    parse_broadcast,
    rewrite_images,
    send_one,
)

GRAPH_USERS_BATCH_SIZE = 999
# Per-recipient pacing — slower than BCC because every email is its own
# /emails:send call. Keeps us well under the ACS 30/min default rate cap.
SEND_PAUSE_SECONDS = 1.0


def fetch_all_members(graph_token: str, ext_app_id: str) -> list[dict]:
    """ALL enabled CIAM users with an email — no opt-in filter."""
    attr_name = f"extension_{ext_app_id}_marketingOptInAt"
    base = "https://graph.microsoft.com/v1.0/users"
    params = {
        "$select": f"id,mail,{attr_name}",
        "$filter": "accountEnabled eq true",
        "$count": "true",
        "$top": str(GRAPH_USERS_BATCH_SIZE),
    }
    url = f"{base}?{urllib.parse.urlencode(params)}"
    headers = {
        "Authorization": f"Bearer {graph_token}",
        "ConsistencyLevel": "eventual",
    }
    members: list[dict] = []
    while url:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
        for u in data.get("value", []):
            mail = u.get("mail")
            if not mail:
                continue
            members.append(
                {
                    "oid": u["id"],
                    "address": mail,
                    "opted_in": u.get(attr_name) is not None,
                }
            )
        url = data.get("@odata.nextLink")
    return members


def build_plain_text_service(fm: dict, body_md: str) -> str:
    return (
        f"{fm.get('subject', '')}\n\n"
        f"{body_md.strip()}\n\n"
        "--\n"
        "Je ontvangt deze e-mail omdat je een L-GEVITY-account hebt. "
        "Dit is een eenmalige service-mededeling — geen marketing.\n"
    )


def main() -> None:
    dry_run = env("DRY_RUN").lower() == "true"
    graph_token = env("GRAPH_TOKEN")
    acs_token = env("ACS_TOKEN")
    ext_app_id = env("EXT_APP_ID")
    acs_endpoint = env("ACS_ENDPOINT")
    sender_address = env("SENDER_ADDRESS")
    sender_display_name = env("SENDER_DISPLAY_NAME", required=False, default="L-GEVITY")
    reply_to = env("REPLY_TO_ADDRESS")
    repo = env("REPO_FULL_NAME")
    ref = env("REF")
    file_path = env("FILE")
    limit_str = env("LIMIT", required=False, default="")
    limit = int(limit_str) if limit_str else None
    recipients_override_raw = env("RECIPIENTS", required=False, default="")

    print(f"DRY_RUN: {dry_run}")
    print(f"File: {file_path}")
    print(f"Sender: {sender_display_name} <{sender_address}>")

    path = Path(file_path)
    if not path.exists():
        sys.exit(f"ERROR: file not found: {path}")

    fm, body_md = parse_broadcast(path)

    # Safety guard — only files explicitly marked transactional. Without
    # this, an accidentally-misrouted marketing file could spam every member.
    if fm.get("kind") != "transactional":
        sys.exit(
            "ERROR: this dispatcher only sends files with frontmatter "
            f"`kind: transactional`. The file has kind={fm.get('kind')!r}. "
            "If this is a marketing broadcast, use the broadcasts/ folder "
            "with send-broadcast.yml instead."
        )

    if recipients_override_raw.strip():
        addresses = [
            line.strip()
            for line in recipients_override_raw.replace(",", "\n").split("\n")
            if line.strip() and "@" in line.strip()
        ]
        if not addresses:
            sys.exit("ERROR: RECIPIENTS env was set but contained no valid addresses.")
        members = [
            {"oid": "explicit", "address": addr, "opted_in": False}
            for addr in addresses
        ]
        print(
            f"Explicit recipient list provided — bypassing Graph query. "
            f"{len(members)} address(es)."
        )
    else:
        print("Fetching ALL CIAM members from Microsoft Graph...")
        members = fetch_all_members(graph_token, ext_app_id)
        total_members = len(members)
        opted_in = sum(1 for m in members if m["opted_in"])
        print(f"Total members with email: {total_members}")
        print(f"  of which already opted in to marketing: {opted_in}")

    if not members:
        print("No members — nothing to send.")
        return

    md_renderer = md.Markdown(extensions=["extra", "sane_lists"])
    rendered_body = md_renderer.reset().convert(body_md)
    rendered_body = rewrite_images(rendered_body, repo, ref)
    subject = fm["subject"]
    # Transactional sends pass empty unsubscribe_url. The footer template
    # branches on `kind` to suppress the marketing-style unsubscribe line.
    html = build_email_html(rendered_body, fm, "")
    text = build_plain_text_service(fm, body_md)

    recipients = members[:limit] if limit else members
    if limit:
        print(f"Pilot mode: capping send to first {limit} recipient(s).")

    sent = 0
    failed = 0
    total = len(recipients)
    for i, recipient in enumerate(recipients, start=1):
        payload = {
            "senderAddress": sender_address,
            "recipients": {"to": [{"address": recipient["address"]}]},
            "content": {"subject": subject, "html": html, "plainText": text},
            "replyTo": [{"address": reply_to, "displayName": sender_display_name}],
            "userEngagementTrackingDisabled": True,
        }

        if dry_run:
            # No PII in logs (public repo).
            print(f"  [DRY] would send {i}/{total}")
            sent += 1
            continue

        try:
            op_id = send_one(acs_token, acs_endpoint, payload)
            sent += 1
            print(f"  sent {i}/{total} -> operation {op_id}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {i}/{total}: {e}", file=sys.stderr)
            print("Halting on first failure.", file=sys.stderr)
            break

        time.sleep(SEND_PAUSE_SECONDS)

    print(f"\nDone: {sent} sent, {failed} failed (of {total}).")
    if dry_run:
        print("\n[DRY] HTML preview (first 600 chars):")
        print(html[:600])
    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
