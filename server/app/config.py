"""Runtime configuration, read once at import."""
from __future__ import annotations

import os
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
SKILLS_DIR = SERVER_DIR / "skills"
#: Declarative agent topology (master prompt, subagents, models, tools, skills).
AGENTS_FILE = Path(os.getenv("AGENTS_FILE", str(SERVER_DIR / "agents.json")))

PORT = int(os.getenv("PORT", "8088"))
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
#: Where the Copilot SDK keeps session state. The hosted platform sets
#: HOME=/home/session, a mount the container user cannot write to, so leaving
#: the SDK on its $HOME default fails with EACCES on the first turn. Defaults
#: to a directory the image creates and owns; override for local runs.
COPILOT_HOME = os.getenv("COPILOT_HOME", str(SERVER_DIR / ".copilot"))
COPILOT_MODEL = os.getenv("COPILOT_MODEL", "claude-sonnet-5")

# Foundry injects these in the hosted container.
APPINSIGHTS = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "")
FOUNDRY_AGENT_NAME = os.getenv("FOUNDRY_AGENT_NAME", "ghcp-sdk-browser")
FOUNDRY_AGENT_VERSION = os.getenv("FOUNDRY_AGENT_VERSION", "local")
#: Data-plane endpoint used to pull skill packages at runtime. Injected as
#: FOUNDRY_PROJECT_ENDPOINT in the hosted container; azd writes the same value
#: locally, so a dev run reads the same skills as production.
FOUNDRY_PROJECT_ENDPOINT = os.getenv("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
#: Set to "0" to skip the Foundry fetch and use only the skills in the image.
FOUNDRY_SKILLS_ENABLED = os.getenv("FOUNDRY_SKILLS_ENABLED", "1") != "0"

BROWSER_TOOL_TIMEOUT_S = float(os.getenv("BROWSER_TOOL_TIMEOUT_S", "45"))
#: Hard bound on the Foundry skill fetch during session creation. The platform
#: gives up on a session well before Azure SDK retry chains do, so this must
#: stay small -- a missed fetch only costs us the newest skill text.
SKILLS_FETCH_TIMEOUT_S = float(os.getenv("SKILLS_FETCH_TIMEOUT_S", "15"))
