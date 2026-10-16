FROM ghcr.io/astral-sh/uv:0.9.3 AS uv
FROM python:3.14-slim
COPY --from=uv /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable
HEALTHCHECK --interval=5m --timeout=20s --start-period=2m --retries=2 CMD ["procareberry", "healthcheck"]
ENTRYPOINT ["procareberry"]
CMD ["serve"]
