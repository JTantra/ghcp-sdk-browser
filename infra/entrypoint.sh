#!/bin/sh
# Runtime bootstrap for the Foundry hosted-agent container.
#
# The platform mounts $HOME (/home/session) at RUNTIME, after the image is
# built -- so a Dockerfile `chown` cannot fix its permissions: the mount does
# not exist yet, and useradd's /home/agent is a different directory entirely.
# That is why COPILOT_HOME previously pointed at /app/.copilot, an image layer
# that resets on every restart, which in turn made resume_session() useless.
#
# Per the docs, $HOME and /files are the ONLY paths the platform persists:
# their contents survive the 15-minute idle deprovision and are restored when
# the session resumes. So point the SDK's state there when it is writable.
set -e

STATE_HOME="${HOME:-/home/session}"

# Probe writability rather than assume it: if the mount is read-only for uid
# 1000 we must fall back, or the app dies at startup with EACCES and every
# session fails with `session_not_ready`.
if mkdir -p "$STATE_HOME/.copilot" 2>/dev/null \
   && [ -w "$STATE_HOME/.copilot" ]; then
    export COPILOT_HOME="$STATE_HOME/.copilot"
    export XDG_CACHE_HOME="$STATE_HOME/.cache"
    export XDG_CONFIG_HOME="$STATE_HOME/.config"
    export XDG_DATA_HOME="$STATE_HOME/.local/share"
    mkdir -p "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" 2>/dev/null || true
    echo "entrypoint: session state -> $COPILOT_HOME (persistent)"
else
    # Ephemeral: conversation history will not survive an idle deprovision,
    # but the agent still works. Logged loudly so this is visible in App
    # Insights rather than silently degrading resume.
    export COPILOT_HOME=/app/.copilot
    export XDG_CACHE_HOME=/app/.cache
    export XDG_CONFIG_HOME=/app/.config
    export XDG_DATA_HOME=/app/.local/share
    echo "entrypoint: WARNING $STATE_HOME not writable; state -> /app (ephemeral, no resume)"
fi

exec python -m app.main
