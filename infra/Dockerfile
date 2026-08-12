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
# NOTE: server/skills is deliberately NOT copied. Skills are fetched from
# Foundry at session start (see app/skills_store.py) so a playbook can be
# updated without rebuilding the image. The folder stays in the repo as the
# publish source for scripts/publish_skills.py.
# Verified before removing: with the folder absent, resolve_skill_directories()
# still reports source="foundry" with all four skills and hands the SDK a real
# directory. Because there is no longer an on-disk fallback, a fetch failure is
# now surfaced loudly by skills_status() instead of silently degrading.
# Declarative agent topology (master prompt, subagents, models, tools, skills).
# agents.py opens this at MODULE LOAD, so omitting it raises FileNotFoundError
# before uvicorn binds -- /readiness never answers and every session fails with
# `session_not_ready`.
COPY server/agents.json ./agents.json

# Non-root: the platform mounts $HOME as the session filesystem.
RUN useradd -m -u 1000 agent && chown -R agent:agent /app

# Fallback state dirs. The entrypoint prefers $HOME (the platform's PERSISTENT
# session mount, restored after an idle deprovision) and only falls back here
# when that mount is not writable. A Dockerfile chown cannot prepare $HOME:
# the platform mounts it at runtime, long after this layer is built.
ENV XDG_CACHE_HOME=/app/.cache \
    XDG_CONFIG_HOME=/app/.config \
    XDG_DATA_HOME=/app/.local/share \
    COPILOT_HOME=/app/.copilot
RUN mkdir -p /app/.cache /app/.config /app/.local/share /app/.copilot \
 && chown -R agent:agent /app/.cache /app/.config /app/.local/share /app/.copilot

COPY infra/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Deliberately NOT `USER agent`. The platform mounts $HOME at runtime as
# uid=0 mode=0755 (measured on the live container), so only root can chown it
# -- and that mount is the only storage that survives an idle deprovision.
# entrypoint.sh chowns it and then drops to `agent` via su, so the Python
# process still runs unprivileged.
EXPOSE 8088
CMD ["/app/entrypoint.sh"]
