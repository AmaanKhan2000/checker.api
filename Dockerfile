FROM python:3.12-slim-bookworm AS builder
WORKDIR /app
RUN pip install --no-cache-dir uv==0.12.8
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra demo --no-editable

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH"
RUN groupadd --gid 10001 sentinel && useradd --uid 10001 --gid sentinel --no-create-home sentinel
WORKDIR /app
COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv
COPY --chown=10001:10001 examples ./examples
RUN mkdir /app/reports /data && chown -R 10001:10001 /app/reports /data
USER 10001:10001
ENTRYPOINT ["sentinel"]
CMD ["--help"]
