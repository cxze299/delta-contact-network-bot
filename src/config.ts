import { resolve } from "node:path";
export function configuration(env: NodeJS.ProcessEnv = process.env) {
  const secret = env.CONTACTBOT_SECRET || "";
  if (secret.length < 32)
    throw Error("CONTACTBOT_SECRET must contain at least 32 characters");
  const verifier = env.CONTACTBOT_XDC_ADMIN_VERIFIER || "";
  if (!/^250000:[a-f0-9]{32}:[a-f0-9]{64}$/.test(verifier))
    throw Error("Valid administrator verifier required");
  const webPassword = env.CONTACTBOT_ADMIN_WEB_TOKEN || "";
  if (webPassword.length < 12)
    throw Error("Web administrator password required");
  return {
    secret,
    verifier,
    webPassword,
    db: resolve(env.CONTACTBOT_BUN_DB || "./data/bun-contactbot.sqlite3"),
    accounts: resolve(env.CONTACTBOT_ACCOUNTS_DIR || "./accounts"),
    rpc: env.CONTACTBOT_RPC_SERVER || "deltachat-rpc-server",
    accountId: Number(env.CONTACTBOT_ACCOUNT_ID) || 0,
    host: env.HOST || "0.0.0.0",
    port: Number(env.PORT) || 8787,
    assets: resolve(env.CONTACTBOT_ASSETS || "./webxdc"),
    requireVerified: env.CONTACTBOT_REQUIRE_VERIFIED === "true",
  };
}
export type Config = ReturnType<typeof configuration>;
