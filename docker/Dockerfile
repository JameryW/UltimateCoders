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

COPY . .
RUN maturin develop --release

# ── Stage 2: Runtime image ─────────────────────────────────────────
FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    libssl3 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy installed Python packages (includes _uc_core .so + all deps)
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages

# Copy the Python source (maturin develop links to the source tree)
COPY python/ultimate_coders /app/python/ultimate_coders

ENV PYTHONPATH=/app
