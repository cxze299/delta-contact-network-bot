/* Authenticated dual transport. UI functions below this block are unchanged. */
(() => {
  const api = window.webxdc,
    token = window.CONTACT_CONFIG?.token;
  if (!api || !token || !crypto.subtle) return;
  const originalListen = api.setUpdateListener.bind(api),
    originalSend = api.sendUpdate.bind(api);
  let channel = null,
    active = false,
    listener = null,
    current = null,
    version = null,
    sequence = 0;
  let lastRealtime = 0, synced = false;
  const connectionStatus = () => {
    const offline = window.navigator?.onLine === false;
    const live = !offline && lastRealtime > 0 && Date.now() - lastRealtime < 45000;
    const label = offline ? "离线" : live ? "实时在线" : synced ? "消息连接" : "连接中";
    for (const el of window.document?.querySelectorAll(".connection-status") || []) {
      el.textContent = label; el.dataset.live = String(live);
    }
  };
  window.XdcConnectionStatus = connectionStatus;
  window.addEventListener?.("online", connectionStatus);
  window.addEventListener?.("offline", connectionStatus);
  let queue = Promise.resolve(),
    initializing = true,
    finalInitial = null,
    resyncFor = null;
  const encoder = new TextEncoder(),
    decoder = new TextDecoder();
  const key = crypto.subtle
    .digest("SHA-256", encoder.encode("friend-assistant-v2:" + token))
    .then((raw) =>
      crypto.subtle.importKey("raw", raw, "AES-GCM", false, [
        "encrypt",
        "decrypt",
      ]),
    );
  const bytes = (value) => Uint8Array.from(atob(value), (c) => c.charCodeAt(0));
  const base64 = (value) => btoa(String.fromCharCode(...new Uint8Array(value)));
  async function requestSnapshot(base) {
    if (resyncFor === base) return;
    resyncFor = base;
    const id = Array.from(crypto.getRandomValues(new Uint8Array(16)), (x) =>
      x.toString(16).padStart(2, "0"),
    ).join("");
    const nonce = crypto.getRandomValues(new Uint8Array(12));
    const body = await crypto.subtle.encrypt(
      {
        name: "AES-GCM",
        iv: nonce,
        additionalData: encoder.encode("contact-action-v2"),
      },
      await key,
      encoder.encode(
        JSON.stringify({
          token,
          id,
          action: "refresh",
          notification_version: 1,
        }),
      ),
    );
    await originalSend(
      {
        payload: {
          type: "contact-action-v2",
          nonce: base64(nonce),
          body: base64(body),
        },
      },
      "",
    );
  }
  async function receive(envelope, realtime) {
    if (
      envelope?.type !== "contact-response-v3" ||
      typeof envelope.body !== "string" ||
      envelope.body.length > 500000
    )
      return;
    let packet;
    try {
      const raw = await crypto.subtle.decrypt(
        {
          name: "AES-GCM",
          iv: bytes(envelope.nonce),
          additionalData: encoder.encode("contact-response-v3"),
        },
        await key,
        bytes(envelope.body),
      );
      packet = JSON.parse(decoder.decode(raw));
    } catch {
      return;
    }
    if (
      packet.role !== "bot" ||
      !Number.isSafeInteger(packet.sequence) ||
      packet.sequence <= sequence
    )
      return;
    synced = true;
    if (realtime) { active = true; lastRealtime = Date.now(); }
    connectionStatus();
    if (packet.state) current = packet.state;
    else if (current && packet.base === version && packet.patch)
      current = { ...current, ...packet.patch };
    else {
      await requestSnapshot(packet.base);
      return;
    }
    version = packet.version;
    sequence = packet.sequence;
    resyncFor = null;
    const update = { serial: sequence, payload: current };
    if (!initializing)
      for (const notification of current.notifications || [])
        listener?.({
          payload: { type: "contact-notification", ...notification },
        });
    if (initializing) finalInitial = update;
    else listener?.(update);
  }
  function enqueue(payload, realtime) {
    queue = queue.then(() => receive(payload, realtime)).catch(() => {});
  }
  window.XdcTransportListen = async (callback) => {
    listener = callback;
    await originalListen((update) => enqueue(update.payload, false), 0);
    await queue;
    initializing = false;
    if (finalInitial) {
      for (const notification of finalInitial.payload.notifications || [])
        listener({
          payload: { type: "contact-notification", ...notification },
        });
      listener(finalInitial);
      finalInitial = null;
    }
    if (typeof api.joinRealtimeChannel === "function") {
      try {
        channel = api.joinRealtimeChannel();
        channel.setListener((data) => {
          try {
            enqueue(JSON.parse(decoder.decode(data)), true);
          } catch {}
        });
        channel.send(
          encoder.encode(JSON.stringify({ type: "contact-ping-v3" })),
        );
        let lastPing = Date.now();
        const heartbeat = setInterval(() => {
          connectionStatus();
          if (active && Date.now() - lastRealtime >= 45000) active = false;
          if (!channel) return;
          const delay = active ? 20000 : 1500;
          if (Date.now() - lastPing < delay) return;
          lastPing = Date.now();
          try {
            channel.send(
              encoder.encode(JSON.stringify({ type: "contact-ping-v3" })),
            );
          } catch {
            active = false;
          }
        }, 1000);
        window.addEventListener(
          "pagehide",
          () => {
            clearInterval(heartbeat);
            try {
              channel?.leave();
            } catch {}
            channel = null;
          },
          { once: true },
        );
      } catch {
        channel = null;
      }
    }
  };
  window.XdcTransportSend = async (payload, kind) => {
    if (channel) {
      try {
        channel.send(encoder.encode(JSON.stringify(payload)));
      } catch {
        active = false;
      }
    }
    // Durable commands always keep the same request id across both channels.
    await originalSend({ payload }, "");
  };
})();
