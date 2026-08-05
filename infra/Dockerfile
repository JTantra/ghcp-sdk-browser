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

# Non-root: the platform mounts $HOME as the session filesystem.
RUN useradd -m -u 1000 agent && chown -R agent:agent /app
USER agent

EXPOSE 8088
CMD ["python", "-m", "app.main"]
