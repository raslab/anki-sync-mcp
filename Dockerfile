FROM ghcr.io/astral-sh/uv:0.11.1 AS uv

FROM python:3.12-slim-bookworm
COPY --from=uv /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

RUN groupadd --system --gid 10001 anki-mcp \
    && useradd --system --uid 10001 --gid anki-mcp --home-dir /app anki-mcp \
    && mkdir -p /app /data \
    && chown anki-mcp:anki-mcp /app /data

WORKDIR /app
COPY --chown=anki-mcp:anki-mcp pyproject.toml uv.lock README.md ./
COPY --chown=anki-mcp:anki-mcp src ./src
RUN uv sync --frozen --no-dev --no-cache

USER 10001:10001
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD ["python", "-m", "anki_mcp.healthcheck"]
CMD ["anki-mcp"]
