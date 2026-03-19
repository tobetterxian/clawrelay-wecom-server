FROM python:3.12-slim

WORKDIR /app

# Base runtime dependencies
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create necessary directories
RUN mkdir -p \
    logs \
    /workspace \
    /data/workspaces \
    /data/codex-home \
    /data/claude-home \
    /run/local-secrets

CMD ["python", "main.py"]
