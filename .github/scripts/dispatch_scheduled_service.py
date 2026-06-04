#!/usr/bin/env python3
"""Scheduled dispatcher for transactional service announcements.

Scans service/*.md for a due `scheduledAt` frontmatter timestamp and dispatches
each file through dispatch_service.py. A sent marker is written after a live
success so cron reruns are idempotent.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path


SERVICE_DIR = Path("service")
SENT_DIR = Path(".dispatch-log/service")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def parse_frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end == -1:
        return {}
    out: dict[str, str] = {}
    for raw_line in text[4:end].splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if (
            (value.startswith("'") and value.endswith("'"))
            or (value.startswith('"') and value.endswith('"'))
        ):
            value = value[1:-1]
        out[key.strip()] = value.replace("''", "'")
    return out


def parse_scheduled_at(raw: object, path: Path) -> datetime | None:
    if raw in (None, ""):
        return None
    if not isinstance(raw, str):
        raise ValueError(f"{path}: scheduledAt must be an ISO-8601 string")
    normalized = raw.strip().replace("Z", "+00:00")
    scheduled_at = datetime.fromisoformat(normalized)
    if scheduled_at.tzinfo is None:
        raise ValueError(f"{path}: scheduledAt must include a timezone offset")
    return scheduled_at.astimezone(UTC)


def marker_path(path: Path) -> Path:
    return SENT_DIR / f"{path.name}.sent.json"


def candidate_files() -> list[Path]:
    requested = env("SCHEDULE_FILE")
    if requested:
        path = Path(requested)
        if len(path.parts) < 2 or path.parts[0] != SERVICE_DIR.name or path.suffix != ".md":
            raise ValueError("SCHEDULE_FILE must point to service/*.md")
        return [path]
    return sorted(SERVICE_DIR.glob("*.md"))


def due_files(now: datetime) -> list[tuple[Path, datetime]]:
    due: list[tuple[Path, datetime]] = []
    for path in candidate_files():
        if marker_path(path).exists():
            continue
        fm = parse_frontmatter(path)
        if fm.get("kind") != "transactional":
            continue
        scheduled_at = parse_scheduled_at(fm.get("scheduledAt"), path)
        if scheduled_at is None:
            continue
        if scheduled_at <= now:
            due.append((path, scheduled_at))
    return due


def dispatch(path: Path) -> None:
    child_env = os.environ.copy()
    child_env["FILE"] = path.as_posix()
    child_env["DRY_RUN"] = "false" if env("CONFIRM").lower() == "true" else "true"
    if env("LIMIT"):
        child_env["LIMIT"] = env("LIMIT")
    if env("RECIPIENTS"):
        child_env["RECIPIENTS"] = env("RECIPIENTS")
    subprocess.run(
        [sys.executable, ".github/scripts/dispatch_service.py"],
        env=child_env,
        check=True,
    )


def write_marker(path: Path, scheduled_at: datetime) -> None:
    SENT_DIR.mkdir(parents=True, exist_ok=True)
    marker = {
        "file": path.as_posix(),
        "scheduledAt": scheduled_at.isoformat(),
        "sentAt": datetime.now(UTC).isoformat(),
        "githubRunId": env("GITHUB_RUN_ID"),
        "githubSha": env("GITHUB_SHA"),
    }
    marker_path(path).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    now = datetime.now(UTC)
    dry_run = env("CONFIRM").lower() != "true"
    print(f"Scheduled service dispatcher at {now.isoformat()}")
    print(f"Mode: {'dry-run' if dry_run else 'LIVE SEND'}")

    due = due_files(now)
    if not due:
        print("No due service announcements.")
        return

    for path, scheduled_at in due:
        print(f"\n=== {path.as_posix()} due {scheduled_at.isoformat()} ===")
        dispatch(path)
        if dry_run:
            print("[DRY] sent marker not written.")
        else:
            write_marker(path, scheduled_at)
            print(f"Sent marker written: {marker_path(path).as_posix()}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
