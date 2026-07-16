# ── Stage 1: Build the Rust extension via maturin ──────────────────
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y \
    curl build-essential pkg-config libssl-dev protobuf-compiler git \
    && rm -rf /var/lib/apt/lists/*

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"

RUN pip install maturin

WORKDIR /app

# ponytail: copy pyproject first + install pure-Python deps so they cache
# separately from Rust/Python source. Source changes won't reinstall fastapi etc.
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    "fastapi>=0.100.0" "uvicorn>=0.20.0" "jinja2>=3.1.0" \
    "sse-starlette>=1.6.0" "nats-py>=2.7.0"

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
RUN maturin develop --release

# ── Stage 2: Runtime image ─────────────────────────────────────────
FROM python:3.11-slim

# Runtime deps: git is required for WorkspaceManager remote sync
# (clone/fetch/push). openssh-client is cheap and enables SSH-based
# remotes as an alternative to HTTPS+PAT. nodejs + npm are needed by
# multilspy's TypeScriptLanguageServer (it shells out to node/npm to
# install typescript-language-server into its static dir).
RUN apt-get update && apt-get install -y \
    bash libssl3 ca-certificates curl git openssh-client nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# git-askpass helper: git invokes this only when auth is needed.
# Reads the PAT from UC_GIT_TOKEN (never embedded in .git/config).
# Set GIT_ASKPASS=/usr/local/bin/git-askpass to enable.
RUN printf '#!/bin/sh\ncase "$1" in\n  Username*) echo "${GIT_USERNAME:-x-access-token}";;\n  Password*) echo "${UC_GIT_TOKEN}";;\nesac\n' \
      > /usr/local/bin/git-askpass \
    && chmod +x /usr/local/bin/git-askpass

WORKDIR /app

# Copy installed Python packages (includes _uc_core .so + all deps)
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages

# Copy rust-analyzer binary built in Stage 1 (multilspy downloads its own
# copy, but having it in PATH is a useful fallback). Non-fatal if absent.
COPY --from=builder /usr/local/bin/rust-analyzer /usr/local/bin/rust-analyzer

# TypeScript LSP: install globally so multilspy (which needs node/npm in
# PATH) can bootstrap typescript-language-server. Non-fatal verification.
RUN npm install -g typescript-language-server typescript \
    && typescript-language-server --version \
    || echo "typescript-language-server install skipped (non-fatal)"

# Grok Build is the default coding agent for Python workers. The installer is
# the official xAI distribution path for the grok-build project; no API key is
# needed at image build time.
RUN curl -fsSL https://x.ai/cli/install.sh | bash
ENV PATH="/root/.local/bin:/root/.grok/bin:${PATH}"
RUN grok --version

# Copy the Python source (maturin develop links to the source tree)
COPY python/ultimate_coders /app/python/ultimate_coders

ENV PYTHONPATH=/app
