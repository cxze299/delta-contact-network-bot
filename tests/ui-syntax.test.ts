import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
test("complete Webxdc source parses before packaging", () => {
  const source = readFileSync("transport.js", "utf8") + "\n" + readFileSync("webxdc/ui.js", "utf8");
  expect(() => new Function(source)).not.toThrow();
  expect(source).not.toContain("\uFFFD");
  expect(source).toContain("正在读取你的网络");
});
