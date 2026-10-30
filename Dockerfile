FROM oven/bun:1.2.23-debian
WORKDIR /app
COPY package.json bun.lock ./
RUN bun install --frozen-lockfile --production
COPY src ./src
COPY schema.sql admin.css admin-base.css admin.js ./
COPY webxdc ./webxdc
COPY scripts ./scripts
ENV CONTACTBOT_BUN_DB=/app/data/bun-contactbot.sqlite3 CONTACTBOT_ACCOUNTS_DIR=/app/account CONTACTBOT_RPC_SERVER=/app/rpc/deltachat-rpc-server CONTACTBOT_ASSETS=/app/webxdc HOST=0.0.0.0 PORT=8787
STOPSIGNAL SIGTERM
ENTRYPOINT ["bun","src/cli.ts"]
CMD ["run"]
