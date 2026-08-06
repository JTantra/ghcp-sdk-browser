"""Runtime configuration, read once at import."""
from __future__ import annotations

import os
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
SKILLS_DIR = SERVER_DIR / "skills"

PORT = int(os.getenv("PORT", "8088"))
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
COPILOT_MODEL = os.getenv("COPILOT_MODEL", "claude-sonnet-5")
OPS_MCP_URL = os.getenv("OPS_MCP_URL", "http://127.0.0.1:8089/mcp")

# Foundry injects these in the hosted container.
APPINSIGHTS = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "")
FOUNDRY_AGENT_NAME = os.getenv("FOUNDRY_AGENT_NAME", "ghcp-sdk-browser")
FOUNDRY_AGENT_VERSION = os.getenv("FOUNDRY_AGENT_VERSION", "local")

BROWSER_TOOL_TIMEOUT_S = float(os.getenv("BROWSER_TOOL_TIMEOUT_S", "45"))
