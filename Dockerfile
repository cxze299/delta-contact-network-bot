FROM python:3.12-slim AS builder
ENV PIP_DEFAULT_TIMEOUT=120
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m venv /venv \
    && /venv/bin/pip install --no-cache-dir .

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=builder /venv /venv
RUN useradd --system --uid 10001 bot \
    && mkdir -p /app/data /app/account \
    && chown -R bot:bot /app
USER bot
ENTRYPOINT ["/venv/bin/contactbot"]
CMD ["run"]
