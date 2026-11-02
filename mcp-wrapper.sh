#!/bin/bash
# Web search MCP server (stdio): websearch.py over SearXNG + FlareSolverr.
# ONLY the python MCP server may write to stdout (the JSON-RPC stream).
# Every wrapper message goes to stderr.

cd "$(dirname "$(readlink -f "$0")")" || exit 1
log() { echo "[websearch-mcp] $*" >&2; }

if ! docker info >/dev/null 2>&1; then
    log "docker daemon not reachable; is the docker service running and are you in the docker group?"
fi

# Generate the instance secret on first run
if [ ! -f .env ]; then
    log "generating .env (first run)..."
    printf "SEARXNG_SECRET=%s\n" "$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')" > .env
fi

# Idempotent: several harnesses (Codex, opencode, Copilot) share these containers,
# so never tear them down here. They stay up until `docker compose down` or reboot.
docker compose up -d >/dev/null 2>&1 || log "docker compose up failed"

# Wait until the HTTP API actually answers (healthz can pass early)
for _ in $(seq 1 60); do
    curl -s -m 3 http://127.0.0.1:8888/healthz >/dev/null 2>&1 && break
    sleep 1
done

log "MCP server ready; serving until session ends."
# Harnesses often launch MCP servers without ~/.local/bin (uv's default install dir) on PATH.
exec "$(command -v uv || echo "$HOME/.local/bin/uv")" run --quiet --script websearch.py
