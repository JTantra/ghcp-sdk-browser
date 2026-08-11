#!/usr/bin/env python3
"""Publish server/skills/* to Microsoft Foundry as first-class Skills.

Registers via the Skills API so they appear under Tools > Skills in the portal
and are usable by any agent in the project:

    POST /skills/{name}/versions        {"inline_content": "<SKILL.md>"}
    header: Foundry-Features: Skills=V1Preview      <- opt-in, else 403

An earlier version of this script wrote dataset blobs directly. That created
the storage (id skill_<sha1>, matching what the platform itself uses) but never
registered the skill, so nothing showed up in the portal. The dataset is an
implementation detail; the Skills API is the real contract.

Usage:
    python3 scripts/publish_skills.py             # publish all
    python3 scripts/publish_skills.py --list      # show what's registered
    python3 scripts/publish_skills.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILLS = REPO / "server" / "skills"
BASE = os.getenv(
    "SKILLS_PROJECT_ENDPOINT",
    "https://jtantra-test-foundry.services.ai.azure.com"
    "/api/projects/jtantra-test-foundry",
).rstrip("/")

#: Skills are preview-gated; without this header every call returns 403
#: preview_feature_required.
PREVIEW = {"Foundry-Features": "Skills=V1Preview"}


def token() -> str:
    out = subprocess.run(
        ["az", "account", "get-access-token", "--resource", "https://ai.azure.com",
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, check=True)
    return out.stdout.strip()


def api(path: str, tok: str, method: str = "GET", body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{BASE}/{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {tok}",
                 "Content-Type": "application/json", **PREVIEW})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:400]


def parse_skill_md(text: str) -> tuple[str, str]:
    """Split a SKILL.md into (description, instructions).

    The Skills API does not take a raw markdown blob: SkillInlineContent
    requires ``description`` and ``instructions`` as separate fields (the
    service told us so -- "was missing required properties including:
    'description', 'instructions'"). So the YAML frontmatter supplies the
    description and the body becomes the instructions.
    """
    description = ""
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            front = text[3:end]
            body = text[end + 4:].lstrip("\n")
            for line in front.splitlines():
                if line.lower().startswith("description:"):
                    description = line.split(":", 1)[1].strip()
                    break
    return description or "(no description)", body.strip() or text.strip()


def publish(skill_dir: Path, tok: str, dry: bool) -> None:
    name = skill_dir.name
    md = skill_dir / "SKILL.md"
    if not md.is_file():
        print(f"  ! {name}: no SKILL.md, skipped")
        return

    description, instructions = parse_skill_md(md.read_text(encoding="utf-8"))
    print(f"  {name}: {description[:60]}")
    if dry:
        return

    # A new version is appended each time; the service assigns the number, so
    # re-running is safe and leaves an audit trail rather than clobbering.
    st, body = api(f"skills/{name}/versions?api-version=v1", tok, "POST",
                   {"inline_content": {"description": description,
                                       "instructions": instructions}})
    if st in (200, 201):
        ver = body.get("version") if isinstance(body, dict) else "?"
        print(f"    published as version {ver}")
    else:
        print(f"    FAILED HTTP {st}: {body}")


def list_skills(tok: str) -> None:
    st, body = api("skills?api-version=v1", tok)
    if st != 200:
        print(f"list failed HTTP {st}: {body}")
        return
    items = body.get("data", []) if isinstance(body, dict) else []
    print(f"{len(items)} skill(s) registered in the project:")
    for s in items:
        print(f"  {s.get('name'):<22} v{s.get('latest_version')}  {s.get('id')}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    tok = token()
    if args.list:
        list_skills(tok)
        return 0

    dirs = sorted(d for d in SKILLS.iterdir() if d.is_dir())
    print(f"publishing {len(dirs)} skill(s) from {SKILLS}")
    for d in dirs:
        publish(d, tok, args.dry_run)
    if not args.dry_run:
        print()
        list_skills(tok)
    return 0


if __name__ == "__main__":
    sys.exit(main())
