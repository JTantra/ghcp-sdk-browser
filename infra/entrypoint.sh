#!/bin/sh
# Runtime bootstrap for the Foundry hosted-agent container.
#
# Runs as ROOT, prepares the persistent session mount, then drops to `agent`.
#
# Why root is required here: the platform mounts $HOME (/home/session) at
# RUNTIME, after the image is built. Measured on the live container, that mount
# is uid=0 gid=0 mode=0755 -- root-owned and writable only by root. A Dockerfile
# `chown` cannot touch it (the mount does not exist at build time), and a
# `USER agent` container cannot chown it either (no CAP_CHOWN at uid 1000).
# So we start as root purely to chown the mount, then hand off with `su`.
#
# This matters because $HOME and /files are the ONLY paths the platform
# persists: their contents survive the 15-minute idle deprovision and are
# restored when the session resumes. With COPILOT_HOME pointed there, an idle
# session keeps its conversation history and resume_session() can reuse it.
set -e

STATE_HOME="${HOME:-/home/session}"
RUN_USER=agent

if [ "$(id -u)" = "0" ]; then
    # Take ownership of the persistent mount so the unprivileged user can
    # write to it. Best-effort: a read-only mount must not break startup.
    chown -R "${RUN_USER}:${RUN_USER}" "$STATE_HOME" 2>/dev/null || true
    exec su -s /bin/sh "$RUN_USER" -c "$0"
fi

# --- unprivileged from here -------------------------------------------------

# Probe writability rather than assume it: if the chown above failed, writing
# blindly kills startup with EACCES, /readiness never answers, and every
# session fails with `session_not_ready`.
if mkdir -p "$STATE_HOME/.copilot" 2>/dev/null \
   && [ -w "$STATE_HOME/.copilot" ]; then
    export COPILOT_HOME="$STATE_HOME/.copilot"
    export XDG_CACHE_HOME="$STATE_HOME/.cache"
    export XDG_CONFIG_HOME="$STATE_HOME/.config"
    export XDG_DATA_HOME="$STATE_HOME/.local/share"
    mkdir -p "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" 2>/dev/null || true
    echo "entrypoint: session state -> $COPILOT_HOME (persistent, resume enabled)"
else
    # Ephemeral: the agent still works, but conversation history dies with the
    # container. Logged loudly so this shows up in App Insights instead of
    # silently disabling resume.
    export COPILOT_HOME=/app/.copilot
    export XDG_CACHE_HOME=/app/.cache
    export XDG_CONFIG_HOME=/app/.config
    export XDG_DATA_HOME=/app/.local/share
    echo "entrypoint: WARNING $STATE_HOME not writable; state -> /app (ephemeral, no resume)"
fi

exec python -m app.main
