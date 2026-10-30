import {
  createHash,
  createHmac,
  createCipheriv,
  createDecipheriv,
  randomBytes,
  timingSafeEqual,
} from "node:crypto";
export const random = (bytes = 24) => randomBytes(bytes).toString("base64url");
export const hash = (value: string) =>
  createHash("sha256").update(value).digest("hex");
export const hmac = (key: string | Buffer, value: string) =>
  createHmac("sha256", key).update(value).digest("hex");
export function equal(a: string, b: string) {
  const x = Buffer.from(a),
    y = Buffer.from(b);
  return x.length === y.length && timingSafeEqual(x, y);
}
export function encrypt(
  value: unknown,
  token: string,
  type = "contact-response-v3",
) {
  const key = createHash("sha256")
      .update("friend-assistant-v2:" + token)
      .digest(),
    nonce = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", key, nonce);
  cipher.setAAD(Buffer.from(type));
  const body = Buffer.concat([
    cipher.update(JSON.stringify(value)),
    cipher.final(),
    cipher.getAuthTag(),
  ]);
  return {
    type,
    nonce: nonce.toString("base64"),
    body: body.toString("base64"),
  };
}
export function decrypt(
  payload: any,
  token: string,
  type = "contact-action-v2",
): any {
  if (
    !payload ||
    payload.type !== type ||
    typeof payload.nonce !== "string" ||
    typeof payload.body !== "string" ||
    payload.body.length > 48000
  )
    throw Error("Invalid envelope");
  const nonce = Buffer.from(payload.nonce, "base64"),
    raw = Buffer.from(payload.body, "base64");
  if (nonce.length !== 12 || raw.length < 16) throw Error("Invalid envelope");
  const key = createHash("sha256")
    .update("friend-assistant-v2:" + token)
    .digest();
  const decipher = createDecipheriv("aes-256-gcm", key, nonce);
  decipher.setAAD(Buffer.from(type));
  decipher.setAuthTag(raw.subarray(-16));
  return JSON.parse(
    Buffer.concat([
      decipher.update(raw.subarray(0, -16)),
      decipher.final(),
    ]).toString(),
  );
}
