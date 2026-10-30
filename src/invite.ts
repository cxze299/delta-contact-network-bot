export function invitationLink(qr: string): string {
  if (qr.startsWith("https://i.delta.chat/#")) return qr;
  if (qr.startsWith("OPENPGP4FPR:"))
    return (
      "https://i.delta.chat/#" +
      qr.slice("OPENPGP4FPR:".length).replace("#", "&")
    );
  throw Error("Unsupported Core invitation format");
}
