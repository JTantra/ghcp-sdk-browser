#!/usr/bin/env python3
"""Publish server/skills/* into Foundry as skill_<sha1> data assets.

Mirrors the layout of the platform-created package we inspected:

    skills/<dataset>/<version>/files/SKILL.md

so ``skills_store.resolve_skill_directories`` can pull them back at runtime.

Usage:
    python3 scripts/publish_skills.py            # publish all
    python3 scripts/publish_skills.py --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILLS = REPO / "server" / "skills"
BASE = (
    "https://jtantra-test-foundry.services.ai.azure.com"
    "/api/projects/jtantra-test-foundry"
)


def token() -> str:
    out = subprocess.run(
        ["az", "account", "get-access-token", "--resource", "https://ai.azure.com",
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, check=True)
    return out.stdout.strip()


def api(url: str, tok: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        method=method)
    with urllib.request.urlopen(req, timeout=90) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def dataset_name(skill: str) -> str:
    """Stable id for a skill, matching the platform's skill_<sha1> convention."""
    return "skill_" + hashlib.sha1(skill.encode()).hexdigest()


def put_blob(sas: str, blob_path: str, payload: bytes) -> None:
    base, query = sas.split("?", 1)
    url = f"{base}/{urllib.parse.quote(blob_path)}?{query}"
    req = urllib.request.Request(
        url, data=payload, method="PUT",
        headers={"x-ms-blob-type": "BlockBlob", "Content-Type": "text/markdown"})
    urllib.request.urlopen(req, timeout=120).read()


def publish(skill_dir: Path, tok: str, dry: bool) -> None:
    name = skill_dir.name
    ds = dataset_name(name)
    files = sorted(p for p in skill_dir.rglob("*") if p.is_file())
    if not files:
        print(f"  ! {name}: no files, skipped")
        return
    print(f"  {name} -> {ds}  ({len(files)} file(s))")
    if dry:
        return

    # Create/replace version 1 of the data asset. pending_upload mints a
    # writable SAS AND registers the asset in one step.
    up = api(
        f"{BASE}/datasets/{ds}/versions/1/startPendingUpload?api-version=v1",
        tok, "POST", {"pendingUploadType": "TemporaryBlobReference"})
    sas = up["blobReference"]["credential"]["sasUri"]

    for f in files:
        rel = f.relative_to(skill_dir).as_posix()
        put_blob(sas, f"skills/{ds}/1/files/{rel}", f.read_bytes())

    api(f"{BASE}/datasets/{ds}/versions/1?api-version=v1", tok, "PUT", {
        "type": "uri_file",
        "dataUri": up["blobReference"]["blobUri"],
        "description": f"Skill package for {name} v1",
    })
    print(f"    published")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    dirs = sorted(d for d in SKILLS.iterdir() if d.is_dir())
    print(f"publishing {len(dirs)} skill(s) from {SKILLS}")
    tok = token()
    for d in dirs:
        publish(d, tok, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
