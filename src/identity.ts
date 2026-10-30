import { createHash } from "node:crypto";
export function fingerprint(encoded: string) {
  const raw = Buffer.from(encoded, "base64");
  if (!raw.length || !(raw[0] & 128)) throw Error("Invalid key");
  let pos = 1,
    size: number,
    tag: number;
  const h = raw[0];
  if (h & 64) {
    tag = h & 63;
    const first = raw[pos++];
    if (first < 192) size = first;
    else if (first < 224) size = ((first - 192) << 8) + raw[pos++] + 192;
    else if (first === 255) {
      size = raw.readUInt32BE(pos);
      pos += 4;
    } else throw Error("Partial packet");
  } else {
    tag = (h >> 2) & 15;
    const kind = h & 3;
    if (kind === 3) throw Error("Invalid packet");
    const len = [1, 2, 4][kind];
    size = raw.readUIntBE(pos, len);
    pos += len;
  }
  const body = raw.subarray(pos, pos + size);
  if (tag !== 6 || body.length !== size) throw Error("Missing public key");
  if (body[0] === 4 && size <= 65535) {
    const prefix = Buffer.alloc(3);
    prefix[0] = 0x99;
    prefix.writeUInt16BE(size, 1);
    return createHash("sha1").update(prefix).update(body).digest("hex");
  }
  if (body[0] === 5 || body[0] === 6) {
    const prefix = Buffer.alloc(5);
    prefix[0] = body[0] === 5 ? 0x9a : 0x9b;
    prefix.writeUInt32BE(size, 1);
    return createHash("sha256").update(prefix).update(body).digest("hex");
  }
  throw Error("Unsupported key");
}
