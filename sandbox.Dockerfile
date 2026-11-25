# Image used to run a PR's checks: no network at runtime, tools baked in.
FROM python:3.12-slim
RUN pip install --no-cache-dir pytest==8.3.4 ruff==0.8.6 \
    && useradd -m runner
ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /work
