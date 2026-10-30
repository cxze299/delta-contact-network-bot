import { spawn } from "node:child_process";
import { StdioDeltaChat } from "@deltachat/jsonrpc-client";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
const folder = mkdtempSync(join(tmpdir(), "friend-rpc-probe-"));
const child = spawn("/app/rpc/deltachat-rpc-server", [], {
  env: { ...process.env, DC_ACCOUNTS_PATH: folder },
  stdio: ["pipe", "pipe", "pipe"],
});
child.stderr.on("data", () => {});
const timer = setTimeout(() => {
  child.kill();
  process.exit(1);
}, 12000);
try {
  const dc = new StdioDeltaChat(child.stdin, child.stdout, false);
  const info = await dc.rpc.getSystemInfo();
  const accounts = await dc.rpc.getAllAccountIds();
  if (accounts.length) throw Error("Probe must use an empty directory");
  console.log("OFFICIAL_RPC_PROBE_OK", Object.keys(info).length);
} finally {
  clearTimeout(timer);
  child.kill();
  setTimeout(() => {
    rmSync(folder, { recursive: true, force: true });
    process.exit(0);
  }, 500);
}
