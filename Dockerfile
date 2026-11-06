# syntax=docker/dockerfile:1

# --- the dashboard: built once, served as static files by api.py ------------
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# --- the toolchain ----------------------------------------------------------
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app
# Dependencies first, so editing a .py file does not reinstall pdfplumber.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project
COPY *.py ./
RUN uv sync --locked
COPY --from=web /web/dist ./web/dist
COPY docker/entrypoint.sh /usr/local/bin/finances-entrypoint

# Code lives in /app, everything personal lives in /data — the only volume.
# Binding 0.0.0.0 is only reachable through the port compose publishes, which
# is the host's loopback; api.py still refuses requests not addressed to localhost.
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    FINANCES_DB=/data/finances.db \
    FINANCES_PDF_DIR=/data/PDFs \
    FINANCES_HOST=0.0.0.0

RUN useradd --create-home --uid 1000 finances \
 && mkdir -p /data/PDFs && chown -R finances /data
USER finances
WORKDIR /data
VOLUME /data
EXPOSE 8787

ENTRYPOINT ["finances-entrypoint"]
