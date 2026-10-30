import { configuration } from "../src/config";
import { Store } from "../src/db";
import { Service } from "../src/service";
import { Admin } from "../src/xdc_admin";
import { createAdmin } from "../src/admin_web";
import { writeFileSync } from "node:fs";
const cfg = configuration();
process.umask(0o077);
const store = new Store(cfg.db),
  service = new Service(store, cfg),
  admin = new Admin(service);
createAdmin(
  service,
  admin,
  () => false,
  () => {},
);
if (!store.one("SELECT 1 FROM networks")) {
  const user = service.user(
    store.one("SELECT id FROM users WHERE contact_id=-1")!.id,
  );
  store.tx(() => admin.action(user, null, "create", { name: "Xhsybyx" }));
  const n = store.one("SELECT * FROM networks ORDER BY id LIMIT 1")!;
  writeFileSync(
    "/app/data/initial-network-access.txt",
    "网络名称：" +
      n.name +
      "\n加入码：" +
      service.readCode(n.join_code_ciphertext) +
      "\n加入码是加入凭证，请勿公开传播。\n",
    { mode: 0o600, flag: "wx" },
  );
}
store.close();
console.log("Fresh business database initialized");
