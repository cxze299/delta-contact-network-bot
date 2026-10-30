import { expect, test } from "bun:test";
import { invitationLink } from "../src/invite";
test("Core URL and OPENPGP invitations normalize without double prefixes", () => {
  expect(invitationLink("https://i.delta.chat/#ABC&a=test")).toBe(
    "https://i.delta.chat/#ABC&a=test",
  );
  expect(invitationLink("OPENPGP4FPR:ABC#a=test")).toBe(
    "https://i.delta.chat/#ABC&a=test",
  );
  expect(() => invitationLink("invalid")).toThrow();
});
