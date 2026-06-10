FROM ubuntu:22.04

# Avoid interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# Install essential packages
RUN apt-get update && apt-get install -y \
    git \
    curl \
    python3 \
    python3-pip \
    nodejs \
    npm \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Install Codex CLI (if available)
# RUN npm install -g @openai/codex

# Create workspace directory
RUN mkdir -p /workspace
WORKDIR /workspace

# Default command (overridden by sandbox executor)
CMD ["/bin/bash"]
