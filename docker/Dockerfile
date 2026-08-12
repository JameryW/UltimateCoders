# ── Stage 1: Build the Rust extension via maturin ──────────────────
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y \
    curl build-essential pkg-config libssl-dev protobuf-compiler git \
    && rm -rf /var/lib/apt/lists/*

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"

RUN pip install maturin

WORKDIR /app

# ponytail: copy pyproject first + install runtime deps so they cache
# separately from Rust/Python source. Source changes won't reinstall fastapi etc.
COPY pyproject.toml ./
# The worker always performs LLM task decomposition before dispatch. Keep the
# optional project extra explicit in the runtime image so a fresh Docker
# deployment does not fall back to newline splitting with ModuleNotFoundError.
RUN pip install --no-cache-dir \
    "fastapi>=0.100.0" "uvicorn>=0.20.0" "websockets>=12.0" "jinja2>=3.1.0" \
    "sse-starlette>=1.6.0" "nats-py>=2.7.0" \
    "anthropic>=0.30.0" "litellm>=1.0.0"

# ponytail: multilspy (optional LSP dep) — install jedi-language-server (pure
# Python, no binary download needed). Verify the import works at build time so
# a broken dep is caught early; if it fails the image still builds (LSP tools
# degrade gracefully at runtime). jedi-language-server ships in site-packages
# which Stage 2 copies to the runtime image.
RUN pip install --no-cache-dir "multilspy==0.0.15" \
    && python -c "import jedi_language_server; from multilspy.language_server import LanguageServer; print('multilspy + jedi OK')" \
    || echo "multilspy install skipped (non-fatal)"

# ponytail: rust-analyzer — builder already has cargo/rustup. Install to a
# known root so we can COPY the binary to the runtime stage. multilspy's
# RustAnalyzer downloads its own copy, but having rust-analyzer in PATH is a
# useful fallback and a build-time smoke check. Ensure the path always exists
# (touch placeholder on failure) so the Stage 2 COPY never breaks the build.
RUN cargo install --root /usr/local --locked rust-analyzer \
    && /usr/local/bin/rust-analyzer --version \
    || (touch /usr/local/bin/rust-analyzer \
        && echo "rust-analyzer install skipped (non-fatal)")

COPY . .
# The repo root Cargo.toml is a virtual workspace (no [package]), so maturin
# must be pointed at the actual Python extension crate. Newer maturin also
# requires a virtualenv for `develop`; point VIRTUAL_ENV at the system prefix
# so it installs into /usr/local/lib/python3.11/site-packages (copied in Stage 2).
RUN VIRTUAL_ENV=/usr/local maturin develop --release --manifest-path crates/uc-python/Cargo.toml

# Use the current Node.js LTS line required by the Claude Code CLI. The
# Debian package in the Python image can lag behind the CLI's engine floor.
FROM node:22-bookworm-slim AS node-runtime

# ── Stage 2: Runtime image ─────────────────────────────────────────
FROM python:3.11-slim

# Replace the official Debian mirror with Tsinghua — deb.debian.org is
# extremely slow (~200 kB/s) from this network, which times out apt during
# the nodejs/npm install. trixie ships deb822 sources in debian.sources;
# we switch to the classic one-line format and drop the old file.
RUN { \
      echo "deb https://mirrors.tuna.tsinghua.edu.cn/debian trixie main contrib non-free non-free-firmware"; \
      echo "deb https://mirrors.tuna.tsinghua.edu.cn/debian trixie-updates main contrib non-free non-free-firmware"; \
      echo "deb https://mirrors.tuna.tsinghua.edu.cn/debian-security trixie-security main contrib non-free non-free-firmware"; \
    } > /etc/apt/sources.list && rm -f /etc/apt/sources.list.d/debian.sources

# Runtime deps: git is required for WorkspaceManager remote sync
# (clone/fetch/push). openssh-client is cheap and enables SSH-based
# remotes as an alternative to HTTPS+PAT. Node.js + npm are copied from
# the dedicated official Node runtime stage above; multilspy's
# TypeScriptLanguageServer shells out to them to install its static server.
RUN apt-get update && apt-get install -y --no-install-recommends \
    -o Acquire::Retries=5 -o Acquire::http::Timeout=60 -o Acquire::https::Timeout=60 \
    bash libssl3 ca-certificates curl git openssh-client util-linux \
    && rm -rf /var/lib/apt/lists/*

# git-askpass helper: git invokes this only when auth is needed.
# Reads the PAT from UC_GIT_TOKEN (never embedded in .git/config).
# Set GIT_ASKPASS=/usr/local/bin/git-askpass to enable.
RUN printf '#!/bin/sh\ncase "$1" in\n  Username*) echo "${GIT_USERNAME:-x-access-token}";;\n  Password*) echo "${UC_GIT_TOKEN}";;\nesac\n' \
      > /usr/local/bin/git-askpass \
    && chmod +x /usr/local/bin/git-askpass

WORKDIR /app

# Copy Node.js 22 + npm from the dedicated official Node runtime stage.
COPY --from=node-runtime /usr/local/ /usr/local/

# Copy installed Python packages (includes _uc_core .so + all deps)
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages

# Copy rust-analyzer binary built in Stage 1 (multilspy downloads its own
# copy, but having it in PATH is a useful fallback). Non-fatal if absent.
COPY --from=builder /usr/local/bin/rust-analyzer /usr/local/bin/rust-analyzer

# TypeScript LSP: install globally so multilspy (which needs node/npm in
# PATH) can bootstrap typescript-language-server.
RUN npm install -g typescript-language-server typescript \
    && typescript-language-server --version

# Install all supported worker CLIs. Authentication is supplied at runtime;
# no provider key is baked into the image.
RUN npm install -g @anthropic-ai/claude-code @openai/codex \
    && claude --version \
    && codex --version

# Grok Build remains the default coding agent for compatibility deployments.
# The installer is the official xAI distribution path; no API key is needed
# at image build time.
RUN curl -fsSL https://x.ai/cli/install.sh | bash
ENV PATH="/root/.local/bin:/root/.grok/bin:${PATH}"
RUN grok --version

# Claude Code refuses --dangerously-skip-permissions under root. Keep the
# container entrypoint root-owned so it can initialize an existing named
# volume, then drop the actual Python worker process to an unprivileged user.
RUN useradd --create-home --uid 1000 --shell /bin/sh ucworker \
    && install -d -o ucworker -g ucworker /home/ucworker/.codex \
    && printf '%s\n' \
      '#!/bin/sh' \
      'set -eu' \
      'if [ "$(id -u)" -eq 0 ] && [ -d /workspace ]; then' \
      '  chown -R ucworker:ucworker /workspace 2>/dev/null || true' \
      '  export HOME=/home/ucworker USER=ucworker LOGNAME=ucworker' \
      '  exec setpriv --reuid=ucworker --regid=ucworker --init-groups "$@"' \
      'fi' \
      'exec "$@"' \
      > /usr/local/bin/uc-worker-entrypoint \
    && chmod +x /usr/local/bin/uc-worker-entrypoint

ENTRYPOINT ["/usr/local/bin/uc-worker-entrypoint"]

# Copy the full Python package from the builder stage — this includes both
# the pure-Python source AND the compiled _uc_core*.so extension that maturin
# develop placed into the source tree. A host-context COPY would lack the .so.
COPY --from=builder /app/python/ultimate_coders /app/python/ultimate_coders

ENV PYTHONPATH=/app
