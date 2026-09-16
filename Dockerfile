FROM python:3.12-slim AS builder
ENV PIP_DEFAULT_TIMEOUT=120
WORKDIR /build
COPY dist/delta_contact_network_bot-0.7.5-py3-none-any.whl /dist/
RUN python -m venv /venv \
    && /venv/bin/pip install --no-cache-dir /dist/delta_contact_network_bot-0.7.5-py3-none-any.whl

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
