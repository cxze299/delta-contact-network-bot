import { spawn } from "node:child_process";
import { configuration } from "./config";
import { Store } from "./db";
import { Service } from "./service";
import { DeltaAdapter } from "./delta_adapter";
import { createAdmin } from "./admin_web";
const config = configuration();
process.umask(0o077);
const store = new Store(config.db),
  service = new Service(store, config),
  adapter = new DeltaAdapter(service, config);
await adapter.start();
const app = createAdmin(
  service,
  adapter.bridge.admin,
  () => adapter.online,
  () => adapter.enqueue(() => adapter.bridge.publishAll()),
  () => adapter.rpc.getChatSecurejoinQrCode(adapter.account, null),
);
const server = Bun.serve({
  hostname: config.host,
  port: config.port,
  fetch: app.fetch,
  maxRequestBodySize: 16384,
});
console.log("Friend Assistant Bun service ready");
let stopping = false;
let backupRunning = false;
function backup() {
  if (!process.env.CONTACTBOT_BACKUP_PASSWORD || backupRunning || stopping)
    return;
  backupRunning = true;
  const child = spawn(process.execPath, ["scripts/backup-db.ts"], {
    stdio: "ignore",
  });
  child.on("error", () => {
    backupRunning = false;
    console.warn("Scheduled backup could not start");
  });
  child.on("exit", (code) => {
    backupRunning = false;
    console.log(
      code === 0
        ? "Encrypted database backup completed"
        : "Encrypted database backup requires review",
    );
  });
}
const firstBackup = setTimeout(backup, 30000),
  backupTimer = setInterval(backup, 24 * 60 * 60 * 1000);

async function stop() {
  if (stopping) return;
  stopping = true;
  clearTimeout(firstBackup);
  clearInterval(backupTimer);
  server.stop();
  const deadline = setTimeout(() => process.exit(1), 12000);
  try {
    await adapter.stop();
    store.close();
    clearTimeout(deadline);
    process.exit(0);
  } catch {
    process.exit(1);
  }
}
process.on("SIGTERM", stop);
process.on("SIGINT", stop);
