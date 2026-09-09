# syntax=docker/dockerfile:1.9
#
# hlmcp — containerized MCP server.
#
# This server speaks MCP over **stdio**: stdin/stdout carry the JSON-RPC stream.
# That drives three decisions that differ from a typical web-service image:
#
#   * no EXPOSE / no port      — there is no socket to publish
#   * no HEALTHCHECK           — the process lives exactly as long as the client
#                                holds the pipe; a periodic probe would have
#                                nothing meaningful to poll
#   * must be run with `-i`    — without stdin attached the transport is dead on
#                                arrival (see "Run" below)
#
# Two stages: the builder resolves and installs dependencies, the runtime keeps
# only the finished virtualenv. No uv, no compiler, no lockfile in the shipped
# image.
#
# Build:
#   docker build -t hlmcp:latest .
#
# Run (note -i, and no -t: the peer is a pipe, not a terminal):
#   docker run -i --rm hlmcp:latest
#
# Claude Desktop (claude_desktop_config.json):
#   "hlmcp": { "command": "docker", "args": ["run", "-i", "--rm", "hlmcp:latest"] }

# ---------------------------------------------------------------- builder ----
FROM python:3.12-slim-bookworm AS builder

# Pinned uv, copied from its official image rather than curl|sh — the version is
# then reproducible and visible in the Dockerfile.
COPY --from=ghcr.io/astral-sh/uv:0.9.20 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    # Build the venv at the SAME absolute path it will occupy at runtime.
    # Virtualenvs are not relocatable: console scripts get an absolute shebang
    # (`#!/app/.venv/bin/python3`) baked in at install time. Building at /build
    # and copying to /app leaves that shebang pointing at a path the runtime
    # stage doesn't have, and the container dies with a confusing
    # "exec ...: no such file or directory" — which refers to the missing
    # *interpreter*, not the script.
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /build

# Dependencies first, in their own layer. They change far less often than source,
# so editing src/ reuses this layer instead of re-resolving the whole tree.
# --frozen: fail if uv.lock is stale rather than silently resolving something
# different from what CI tested.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Now the project itself. README.md and LICENSE are required at build time:
# pyproject declares readme = "README.md" and license-files = ["LICENSE"].
COPY README.md LICENSE ./
COPY src/ ./src/

# --no-editable installs a real copy into site-packages instead of an editable
# install. Deliberate: an editable install ships a .pth file, and .pth resolution
# is exactly the fragility this image should not inherit (CPython's `site` skips
# .pth files carrying the macOS hidden flag — see CLAUDE.md). A copied package is
# found by ordinary path scanning, so there is nothing to skip.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-editable --no-dev

# Smoke-test the artifact while the build can still fail. Constructing the
# FastMCP app exercises the import graph, tool registration, and the packaged
# data file (hlmcp/data/curated_whales.json) — a wheel that omitted package data
# would pass a bare `import hlmcp` and fail here. No network: tool *registration*
# is offline; only tool *invocation* would reach Hyperliquid.
RUN /app/.venv/bin/python -c "\
import asyncio; \
from hlmcp.server import mcp; \
from hlmcp.tools.whale_position_monitor import load_curated_whales; \
tools = asyncio.run(mcp.list_tools()); \
assert len(tools) == 3, f'expected 3 tools, got {len(tools)}'; \
wallets = load_curated_whales(); \
assert wallets, 'curated_whales.json missing or has no wallets in the wheel'; \
print(f'smoke ok: {len(tools)} tools, {len(wallets)} curated wallets')"

# ---------------------------------------------------------------- runtime ----
FROM python:3.12-slim-bookworm AS runtime

# ca-certificates for TLS to api.hyperliquid.xyz. Explicit rather than assumed:
# a missing trust store is a classic containerization failure that only shows up
# on the first outbound HTTPS call, long after the image looks healthy.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged, no login shell, no home-directory writes needed. Fixed uid so
# file ownership is predictable if a volume is ever mounted.
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin hlmcp

# Only the finished virtualenv crosses the stage boundary.
COPY --from=builder --chown=hlmcp:hlmcp /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    # Unbuffered: stdout IS the protocol channel, so responses must not sit in a
    # buffer waiting for it to fill.
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Config is env-driven and every var is optional (see .env.example); defaults are
# the calibrated values in config.py. Override at run time, e.g.
#   docker run -i --rm -e HL_BURST_CAPACITY=20 hlmcp:latest

USER hlmcp
WORKDIR /app

# The console script from pyproject's [project.scripts]. exec form, so the server
# is PID 1 and receives SIGTERM directly for a clean shutdown.
ENTRYPOINT ["hlmcp-server"]
