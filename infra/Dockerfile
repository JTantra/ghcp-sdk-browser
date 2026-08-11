# Foundry Hosted Agent container.
# Contract: listen on 8088, plain HTTP, GET /readiness -> 200, graceful SIGTERM.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8088

# The Copilot SDK spawns the bundled `copilot` CLI as a child process, which
# needs Node at runtime. Without this the SDK starts and then fails on first
# session with a spawn error.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY server/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY server/app ./app
COPY server/skills ./skills
# Declarative agent topology (master prompt, subagents, models, tools, skills).
# agents.py opens this at MODULE LOAD, so omitting it raises FileNotFoundError
# before uvicorn binds -- /readiness never answers and every session fails with
# `session_not_ready`.
COPY server/agents.json ./agents.json

# Non-root: the platform mounts $HOME as the session filesystem.
RUN useradd -m -u 1000 agent && chown -R agent:agent /app

# The platform overrides HOME to /home/session, a mount this user does not own,
# so anything writing under $HOME fails with EACCES. The Copilot SDK caches
# there on first session. Point the XDG dirs at a path we DO own -- setting
# these explicitly is more robust than assuming $HOME is writable.
ENV XDG_CACHE_HOME=/app/.cache \
    XDG_CONFIG_HOME=/app/.config \
    XDG_DATA_HOME=/app/.local/share \
    COPILOT_HOME=/app/.copilot
RUN mkdir -p /app/.cache /app/.config /app/.local/share /app/.copilot \
 && chown -R agent:agent /app/.cache /app/.config /app/.local/share /app/.copilot

USER agent

EXPOSE 8088
CMD ["python", "-m", "app.main"]
