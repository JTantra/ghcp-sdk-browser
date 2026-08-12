"""Fetch skill packages from Microsoft Foundry at session start.

Skills used to be baked into the image (``COPY server/skills ./skills``), so
changing a playbook meant a rebuild + redeploy. They now live in the Foundry
project as data assets and are pulled on demand, which means a skill can be
updated without touching the container.

The Foundry convention -- confirmed against a platform-created package in this
project -- is:

  * a dataset named ``skill_<sha1>``, description ``Skill package for <name> v<n>``
  * ``POST /datasets/{name}/versions/{v}/credentials`` mints a read+list SAS
  * the container holds
        skills/<dataset>/<v>/files/SKILL.md   <- the plain file(s)
        skills/<dataset>/<v>/package.zip      <- the same content, zipped

We read the ``files/`` prefix rather than the zip: it needs no extraction step
and maps directly onto the layout the Copilot SDK expects on disk.

The SDK takes ``skill_directories=[path]`` -- filesystem paths, not content --
so fetched skills are materialised into a temp directory and that path is
handed to the SDK.

Failure is deliberately non-fatal: if Foundry is unreachable we fall back to
whatever shipped in the image, because a degraded agent beats a dead one.
"""

from __future__ import annotations

import logging
import shutil
import threading
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .config import FOUNDRY_PROJECT_ENDPOINT, FOUNDRY_SKILLS_ENABLED, SKILLS_DIR

log = logging.getLogger("skills")

#: Where fetched skills are written. Recreated per process, not per session:
#: skills are project-scoped, so re-downloading for every session would add
#: latency to each turn for no benefit.
_CACHE = Path("/tmp/foundry-skills")

_state: dict[str, Any] = {"source": "image", "skills": [], "error": None}

#: Resolved once per process. Skills are project-scoped, so re-downloading for
#: every session would add latency to each new conversation for no benefit.
_resolved: list[str] | None = None

#: Guards the fetch: /info and session creation can race from separate threads.
_lock = threading.Lock()


def skills_status() -> dict[str, Any]:
    """Where skills came from. Surfaced on /info alongside telemetry_status().

    Adds ``usable`` and escalates the error when NOTHING resolved: the image no
    longer ships a skills folder (see infra/Dockerfile), so the historical
    "fall back to disk" path can now yield zero real directories. Without this
    the agent would run with no skills at all and still report a clean status.
    """
    state = dict(_state)
    dirs = _resolved or []
    real = [d for d in dirs if Path(d).is_dir()]
    state["usable"] = bool(real)
    state["directories"] = real
    if not real and state.get("source") != "foundry":
        state["error"] = (
            f"NO SKILLS AVAILABLE: {state.get('error') or 'fetch did not run'}"
            " (no image fallback -- skills come from Foundry only)"
        )
    return state


def _token() -> str:
    """Bearer token for the Foundry data plane via the container's identity.

    ``azure.identity`` is imported lazily and defensively: an ImportError here
    must degrade to image skills, never take down the whole app. (It once did:
    the package was missing from requirements.txt, so importing this module
    killed uvicorn before it bound, /readiness never answered, and every
    session failed with `session_not_ready`.)

    ``DefaultAzureCredential`` also walks a long chain of sources and, when
    managed identity is unreachable, IMDS probes stall for minutes -- far past
    the platform's session timeout. Prefer managed identity directly.
    """
    try:
        from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
    except ImportError as exc:  # pragma: no cover - depends on image contents
        raise RuntimeError(f"azure-identity unavailable: {exc}") from exc

    try:
        return ManagedIdentityCredential().get_token(
            "https://ai.azure.com/.default"
        ).token
    except Exception:  # noqa: BLE001 - fall back for local runs (az login)
        return DefaultAzureCredential(
            exclude_interactive_browser_credential=True,
        ).get_token("https://ai.azure.com/.default").token


def _get_json(url: str, token: str, method: str = "GET") -> dict:
    data = b"{}" if method == "POST" else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method,
    )
    import json

    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def _list_skill_datasets(token: str) -> list[dict]:
    """Every dataset that follows the skill-package naming convention."""
    url = f"{FOUNDRY_PROJECT_ENDPOINT}/datasets?api-version=v1"
    out = []
    for ds in _get_json(url, token).get("value", []):
        if str(ds.get("name", "")).startswith("skill_"):
            out.append(ds)
    return out


def _skill_name(ds: dict) -> str:
    """Recover the human name from ``Skill package for <name> v<n>``.

    The dataset name itself is only a sha1, so the description carries the
    readable name. Fall back to the raw dataset name if the format changes,
    rather than dropping the skill entirely.
    """
    desc = str(ds.get("description") or "")
    marker = "Skill package for "
    if marker in desc:
        rest = desc.split(marker, 1)[1].strip()
        # Trailing " v1" is a version marker, not part of the name.
        if " v" in rest:
            rest = rest.rsplit(" v", 1)[0]
        if rest:
            return rest
    return str(ds.get("name", "skill"))


def _download_package(ds: dict, token: str, dest_root: Path) -> str | None:
    """Materialise one skill package into ``dest_root/<skill-name>/``."""
    name, version = ds["name"], str(ds.get("version", "1"))
    cred = _get_json(
        f"{FOUNDRY_PROJECT_ENDPOINT}/datasets/{name}/versions/{version}/credentials?api-version=v1",
        token,
        method="POST",
    )
    sas = cred["blobReference"]["credential"]["sasUri"]
    base, query = sas.split("?", 1)

    # List the container, then keep only this package's plain files. The
    # sibling package.zip is skipped -- same content, needs unpacking.
    with urllib.request.urlopen(
        f"{base}?restype=container&comp=list&{query}", timeout=60
    ) as resp:
        blobs = [b.findtext("Name") for b in ET.fromstring(resp.read().decode()).iter("Blob")]

    prefix = f"skills/{name}/{version}/files/"
    wanted = [b for b in blobs if b and b.startswith(prefix)]
    if not wanted:
        log.warning("skill package %s has no files/ entries; skipping", name)
        return None

    skill_dir = dest_root / _skill_name(ds)
    for blob in wanted:
        rel = blob[len(prefix):]
        target = skill_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        url = f"{base}/{urllib.parse.quote(blob)}?{query}"
        with urllib.request.urlopen(url, timeout=120) as resp:
            target.write_bytes(resp.read())
    return _skill_name(ds)


def resolve_skill_directories(refresh: bool = False) -> list[str]:
    """Skill directories for the SDK, preferring Foundry over the image.

    Cached per process; pass ``refresh=True`` to re-pull after editing a skill
    in Foundry without restarting the container.

    Returns the baked-in directory unchanged if anything goes wrong, so a
    Foundry outage degrades the agent's knowledge rather than killing startup.
    """
    global _resolved
    if _resolved is not None and not refresh:
        return _resolved

    # /info and session creation can both call this at once, from different
    # threads (each goes through asyncio.to_thread). Without the lock both
    # download into the same directory and both append to _state["skills"],
    # which is why /info reported every skill twice.
    with _lock:
        if _resolved is not None and not refresh:
            return _resolved
        return _resolve_locked()


def _resolve_locked() -> list[str]:
    global _resolved
    fallback = [str(SKILLS_DIR)]
    if not FOUNDRY_SKILLS_ENABLED:
        _state.update(source="image", skills=[], error="disabled via FOUNDRY_SKILLS_ENABLED=0")
        _resolved = fallback
        return fallback
    if not FOUNDRY_PROJECT_ENDPOINT:
        # A local run without azd env vars: not an error, just no store.
        _state.update(source="image", skills=[], error="FOUNDRY_PROJECT_ENDPOINT unset")
        log.info("no Foundry endpoint configured; using image skills")
        _resolved = fallback
        return fallback
    try:
        token = _token()
        datasets = _list_skill_datasets(token)
        if not datasets:
            _state.update(source="image", skills=[], error="no skill_* datasets in project")
            log.info("no Foundry skill packages found; using image skills")
            _resolved = fallback
            return fallback

        if _CACHE.exists():
            shutil.rmtree(_CACHE, ignore_errors=True)
        _CACHE.mkdir(parents=True, exist_ok=True)

        fetched = [n for ds in datasets if (n := _download_package(ds, token, _CACHE))]
        if not fetched:
            _state.update(source="image", skills=[], error="packages present but empty")
            _resolved = fallback
            return fallback

        # Image skills stay on the list as a base layer: anything not yet
        # migrated to Foundry keeps working.
        _state.update(source="foundry", skills=sorted(fetched), error=None)
        log.info("loaded %d skill(s) from Foundry: %s", len(fetched), ", ".join(sorted(fetched)))
        _resolved = [str(_CACHE), *fallback]
        return _resolved

    except Exception as exc:  # noqa: BLE001 - never fail a session over skills
        _state.update(source="image", skills=[], error=str(exc))
        log.warning("Foundry skill fetch failed (%s); using image skills", exc)
        # NOT cached: a transient outage should not pin us to image skills for
        # the life of the container -- the next session retries.
        return fallback
