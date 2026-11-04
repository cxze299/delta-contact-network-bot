import { createHash } from "node:crypto";
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import type { RawClient } from "@deltachat/jsonrpc-client";

// Core recodes avatars to raster images; keep the SVG as the design source.
export async function syncBotAvatar(rpc: RawClient, account: number, assets: string, accounts: string) {
  const source = join(assets, "icon.png");
  const marker = join(accounts, `avatar-${account}.sha256`);
  const digest = createHash("sha256").update(readFileSync(source)).digest("hex");
  const current = await rpc.getConfig(account, "selfavatar");
  if (current && existsSync(marker) && readFileSync(marker, "utf8") === digest) return;
  await rpc.setConfig(account, "selfavatar", source);
  if (!(await rpc.getConfig(account, "selfavatar"))) throw Error("Bot avatar was not saved");
  writeFileSync(marker, digest, { mode: 0o600 });
}
