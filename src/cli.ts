import { invitationLink } from "./invite";
import { spawn } from "node:child_process";
import { StdioDeltaChat } from "@deltachat/jsonrpc-client";
import { pbkdf2Sync, randomBytes } from "node:crypto";
import { mkdirSync, existsSync } from "node:fs";
import { join } from "node:path";
const command = process.argv[2] || "run";
if (command === "verifier") {
  const key = await Bun.stdin.text(),
    salt = randomBytes(16);
  if (!key) throw Error("Empty administrator key");
  console.log(
    "250000:" +
      salt.toString("hex") +
      ":" +
      pbkdf2Sync(key, salt, 250000, 32, "sha256").toString("hex"),
  );
} else if (command === "run") await import("./main");
else {
  const { configuration } = await import("./config"),
    config = configuration();
  process.umask(0o077);
  if (command === "init-db") {
    const { Store } = await import("./db");
    const s = new Store(config.db);
    s.close();
    console.log("Database initialized");
  } else if (command === "configure" || command === "invite") {
    mkdirSync(config.accounts, { recursive: true, mode: 0o700 });
    const child = spawn(config.rpc, [], {
      env: {
        ...process.env,
        DC_ACCOUNTS_PATH: existsSync(
          join(config.accounts, "accounts", "accounts.toml"),
        )
          ? join(config.accounts, "accounts")
          : config.accounts,
      },
      stdio: ["pipe", "pipe", "pipe"],
    });
    child.stderr.on("data", () => {});
    const rpc = new StdioDeltaChat(child.stdin, child.stdout, false).rpc;
    try {
      const ids = await rpc.getAllAccountIds(),
        id = config.accountId || ids[0] || (await rpc.addAccount());
      if (command === "configure") {
        if (await rpc.isConfigured(id))
          throw Error(
            "Existing account is already configured; refusing replacement",
          );
        const qr = process.argv[3];
        if (!qr?.startsWith("dcaccount:"))
          throw Error("A dcaccount: relay invitation is required");
        await rpc.addTransportFromQr(id, qr);
        if (!(await rpc.isConfigured(id)))
          throw Error("Account configuration incomplete");
        await rpc.setConfig(
          id,
          "displayname",
          process.env.CONTACTBOT_DISPLAY_NAME?.trim() || "好友助手",
        );
        await rpc.setConfig(id, "bot", "1");
      }
      const qr = await rpc.getChatSecurejoinQrCode(id, null);
      console.log(qr);
      console.log(invitationLink(qr));
    } finally {
      child.kill();
    }
  } else throw Error("Unknown command");
}
