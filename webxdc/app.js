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
    if (realtime) active = true;
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

"use strict";
// Identity and permissions come from Core and the server, never selfAddr.
const $ = id => document.getElementById(id);
let state = {networks: [], pending: null, messages: [], requests: [], choices: [], profile: null, current_network: null};
let tab = "好友", filter = "待处理", preparing = false, ready = false, selected = null;
let deferredRemoteRender = false;
let adminView = false, adminSection = "网络", adminConfirm = null, adminUnlocking = false;
let noteTimer, adminResultTimer, adminResultKey = null, adminResultPersistent = false;
let recoveryDraft = {joinCode: "", nickname: "", oldAddress: ""};
const expandedMembers = new Set();
const expandedSettings = new Set();
let notifications = [], readNotifications = {};
try { readNotifications = JSON.parse(localStorage.getItem("friend-notifications-read") || "{}"); } catch { /* Storage is optional. */ }
let waiting = null, waitTimer, submittingControl = null, busyControl = null, lastStateSerial = 0;
let dismissedStep = null;
let joinDraft = null;
let unresolved = [];
try { unresolved = JSON.parse(localStorage.getItem("friend-unresolved-v1") || "[]"); if (!Array.isArray(unresolved)) unresolved = []; } catch { unresolved = []; }
try { waiting = JSON.parse(localStorage.getItem("friend-pending-v2") || "null"); } catch { /* Storage is optional. */ }
if (waiting) waiting.phase = "unknown"; // Reloading never resends an operation.
const bound = Boolean(window.CONTACT_CONFIG?.token);
function node(tag, text, parent, cls = "") {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = text;
  e.className = cls; if (parent) parent.append(e); return e;
}
function button(parent, text, fn, cls = "secondary") {
  const e = node("button", text, parent, cls); e.type = "button"; e.onclick = event => { submittingControl = e; fn(event); }; return e;
}
function visibleText(text) {
  const code = "M[A-HJ-NP-Z2-9]{6}";
  return String(text || "")
    .replace(new RegExp("[（(]" + code + "[）)]", "g"), "")
    .replace(new RegExp("成员编号(?:[：:]\\s*|\\s+)" + code, "g"), "成员资料")
    .replace(new RegExp("(^|\\n)" + code + "[｜|]", "g"), "$1")
    .replace(new RegExp("对象\\s+" + code + "[｜|]?", "g"), "")
    .replace(new RegExp("[｜|]" + code + "(?=[｜|\\s。\\n]|$)", "g"), "")
    .replace(new RegExp("成员\\s+" + code, "g"), "成员");
}
function note(text) {
  clearTimeout(noteTimer);
  text = visibleText(text);
  $("status").textContent = text; $("status").hidden = !text;
  const modalStatus = document.getElementById("modal-status");
  if (modalStatus) { modalStatus.textContent = text; modalStatus.hidden = !text; }
  if (text) noteTimer = setTimeout(() => {
    $("status").hidden = true;
    const current = document.getElementById("modal-status");
    if (current) current.hidden = true;
  }, 3000);
}
function rememberPending() {
  try {
    if (waiting) localStorage.setItem("friend-pending-v2", JSON.stringify(waiting));
    else localStorage.removeItem("friend-pending-v2");
  } catch { /* Transport still works when local storage is unavailable. */ }
}
function rememberUnresolved() {
  try { localStorage.setItem("friend-unresolved-v1", JSON.stringify(unresolved)); } catch { /* Storage is optional. */ }
}
function markUnknown() {
  if (!waiting) return;
  if (!unresolved.some(item => item.id === waiting.id)) unresolved.push(waiting);
  waiting = null; rememberPending(); rememberUnresolved(); operationFeedback(null);
  note("操作结果暂未确认，请稍后查看。");
  render();
}
function syncOperationControls() {
  const busy = Boolean(waiting || preparing);
  document.querySelector("main").setAttribute("aria-busy", String(busy));
  for (const control of document.querySelectorAll("main button,main select,#modal-root button,#modal-root select")) {
    if (control.classList.contains("admin-back") || control.classList.contains("wizard-back") || control.classList.contains("local-navigation") || control.closest(".admin-tabs,.segments")) continue;
    if (busy && !control.disabled) { control.dataset.operationDisabled = "true"; control.disabled = true; }
    if (!busy && control.dataset.operationDisabled) { control.disabled = false; delete control.dataset.operationDisabled; }
  }
}
function operationFeedback(phase) {
  if (busyControl?.isConnected) {
    if (!phase) {
      busyControl.textContent = busyControl.dataset.originalLabel;
      busyControl.disabled = false; busyControl.removeAttribute("aria-busy");
    } else {
      busyControl.textContent = phase === "unknown" ? "结果待确认" : "处理中…";
      busyControl.disabled = true; busyControl.setAttribute("aria-busy", "true");
    }
  }
  if (!phase) busyControl = null;
  syncOperationControls();
}
function watchPending() {
  clearTimeout(waitTimer);
  waitTimer = setTimeout(() => {
    if (!waiting) return;
    markUnknown();
  }, 3000);
}
function toBase64(bytes) { return btoa(String.fromCharCode(...bytes)); }
async function seal(request) {
  const material = await crypto.subtle.digest("SHA-256", new TextEncoder().encode("friend-assistant-v2:" + window.CONTACT_CONFIG.token));
  const key = await crypto.subtle.importKey("raw", material, "AES-GCM", false, ["encrypt"]);
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  const body = await crypto.subtle.encrypt({name: "AES-GCM", iv: nonce, additionalData: new TextEncoder().encode("contact-action-v2")}, key, new TextEncoder().encode(JSON.stringify(request)));
  return {type: "contact-action-v2", nonce: toBase64(nonce), body: toBase64(new Uint8Array(body))};
}
async function resend() {
  if (!waiting) return;
  if (waiting.phase === "sending") return;
  const requestId = waiting.id;
  waiting.phase = "sending"; rememberPending(); operationFeedback("sending");
  watchPending();
  try {
    await window.XdcTransportSend(waiting.payload, waiting.kind);
    if (waiting?.id === requestId && waiting.phase === "sending") { waiting.phase = "waiting"; rememberPending(); }
  } catch { if (waiting?.id === requestId) markUnknown(); }
}
async function send(action, extra = {}) {
  if (!window.webxdc) { note("请在 Delta Chat 内打开好友助手小应用。"); return; }
  if (preparing) return;
  if (!bound) {
    try { await window.webxdc.sendToChat({text: "小程序"}); }
    catch { note("请在好友助手私聊发送一次“小程序”。"); }
    return;
  }
  if (waiting) { note("上一项操作尚未确认，请稍候。"); return; }
  if (bound && !ready) { note("还未收到机器人状态，请稍等；长期无响应可发送“小程序”获取新版。"); return; }
  if (!window.webxdc.sendUpdate || !crypto.subtle) { note("当前客户端缺少所需能力，请更新 Delta Chat。"); return; }
  preparing = true;
  busyControl = submittingControl?.isConnected && submittingControl.tagName === "BUTTON" ? submittingControl : null;
  if (busyControl) busyControl.dataset.originalLabel = busyControl.textContent;
  operationFeedback("sending");
  const operationStep = state.pending?.action;
  const id = Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2, "0")).join("");
  note("正在提交…");
  try {
    const fingerprint = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify({action, network: state.current_network, ...extra})));
    const signature = Array.from(new Uint8Array(fingerprint), n => n.toString(16).padStart(2, "0")).join("");
    if (unresolved.some(item => item.signature === signature || (!item.signature && item.kind === action))) {
      preparing = false; operationFeedback(null); note("此前相同操作尚未确认，请稍后查看。"); return;
    }
    const payload = await seal({token: window.CONTACT_CONFIG.token, id, action, network: state.current_network, notification_version: 1, ...extra});
    waiting = {id, payload, signature, phase: "prepared", step: operationStep, network: state.current_network, kind: action, operation: extra.operation || "", command: extra.command || "", openMessages: action === "command" && extra.command === "申请"}; rememberPending();
    if (action === "join_network" && joinDraft) joinDraft.requestId = id;
    preparing = false;
    await resend();
  } catch { operationFeedback(null); note("提交失败，请稍后重试。"); }
  finally { preparing = false; syncOperationControls(); }
}
const command = (name, value = "") => send("command", {command: name, value});
function form(parent, label, submit, options = {}) {
  const f = node("form", null, parent), l = node("label", label, f);
  const input = node(options.multiline ? "textarea" : "input", null, l);
  if (options.type && !options.multiline) input.type = options.type;
  if (options.autocomplete) input.autocomplete = options.autocomplete;
  input.value = options.value || ""; input.placeholder = options.placeholder || "";
  input.maxLength = options.max || 160; input.required = options.required !== false;
  if (options.multiline) input.rows = 3;
  const b = node("button", options.button || "继续", f, options.buttonClass || "primary"); b.type = "submit";
  f.onsubmit = e => { e.preventDefault(); submittingControl = b; const value = input.value.trim(); if (options.clearOnSubmit) input.value = ""; input.blur(); submit(value); }; return input;
}
function accountFromInput(value) {
  const text = value.trim().replace(/\\&/g, "&");
  const match = text.match(/https:\/\/i\.delta\.chat\/#[^\s<>\]\)]+/i);
  if (!match) return /^[^\s@]{1,64}@[^\s@]{1,190}$/.test(text) ? text : "";
  try {
    const link = new URL(match[0].replace(/[。；，,;.!！]+$/, ""));
    if (link.protocol !== "https:" || link.hostname.toLowerCase() !== "i.delta.chat") return "";
    const [fingerprint, ...query] = link.hash.slice(1).split("&");
    const params = new URLSearchParams(query.join("&"));
    const address = params.get("a") || "";
    if (!/^[0-9a-f]{40}$/i.test(fingerprint) || !["v", "i", "s"].every(k => params.get(k)) || !/^[^\s@]{1,64}@[^\s@]{1,190}$/.test(address)) return "";
    return address;
  } catch { return ""; }
}
function localSwitch(parent, text, initial = true, onChange = () => {}) {
  const row = node("div", null, parent, "setting"); node("span", text, row);
  let enabled = initial;
  const toggle = button(row, "", () => { enabled = !enabled; toggle.setAttribute("aria-checked", String(enabled)); onChange(enabled); }, "ios-switch");
  toggle.setAttribute("role", "switch"); toggle.setAttribute("aria-label", text); toggle.setAttribute("aria-checked", String(enabled));
  node("span", null, toggle, "switch-knob"); return () => enabled;
}
function scrollTop() { document.querySelector("main").scrollTo(0, 0); }
function currentAccount(parent) {
  const address = state.communication_address || state.profile?.communication_address || "";
  node("p", address || "暂时无法读取当前账号，请刷新后重试。", parent, "account-value");
  return address;
}
function disclosure(parent, title) {
  const d = node("details", null, parent, "card"); node("summary", title, d); return d;
}
function settingsDisclosure(parent, title) {
  const section = disclosure(parent, title);
  section.open = expandedSettings.has(title);
  section.addEventListener("toggle", () => {
    if (!section.isConnected) return;
    if (section.open) expandedSettings.add(title); else expandedSettings.delete(title);
  });
  return section;
}
function privacy(parent) {
  node("p", "机器人服务端可以处理你发送的数据。只收集昵称、介绍及必要账号标识；目录不展示联系方式。", parent, "muted");
  node("p", "联系方式仅按你的授权分享；交付后无法从接收方设备收回。小应用更新留存在聊天历史中，删除服务器数据不会自动删除设备上的历史，请自行删除旧的小应用消息。", parent, "muted");
}
function openJoin() {
  joinDraft = {code: "", nickname: state.profile?.nickname || "", discoverable: true, accepts: true, requestId: null};
  dismissedStep = state.pending?.token || null;
  adminView = false; adminConfirm = null; closeModal(); render(); scrollTop();
}
function renderJoin(page) {
  const draft = joinDraft;
  button(page, "‹ 返回", () => { joinDraft = null; tab = state.profile ? "我的" : "好友"; render(); scrollTop(); }, "quiet local-navigation");
  node("h1", "加入新网络", page);
  const f = node("form", null, page, "card onboarding");
  const codeLabel = node("label", "网络加入码", f);
  const code = node("input", null, codeLabel); code.value = draft.code; code.required = true; code.maxLength = 200;
    code.placeholder = "粘贴管理员提供的加入码（不是网络名称）"; code.autocomplete = "off"; code.autocapitalize = "none"; code.spellcheck = false;
    code.setAttribute("autocorrect", "off"); code.oninput = () => { draft.code = code.value; };
  const label = node("label", "在此网络使用的昵称", f);
  const nickname = node("input", null, label); nickname.value = draft.nickname; nickname.required = true; nickname.maxLength = 40;
  nickname.oninput = () => { draft.nickname = nickname.value; };
  node("p", "使用当前账号", f, "muted");
  node("p", state.communication_address || state.profile?.communication_address || "当前与好友助手私聊的 Delta Chat 账号", f, "account-value");
  localSwitch(f, "在好友列表展示我", draft.discoverable, enabled => { draft.discoverable = enabled; });
  localSwitch(f, "接收好友申请", draft.accepts, enabled => { draft.accepts = enabled; });
  node("p", "加入即同意隐私说明并开启名片交换。服务端可处理资料；交付的名片无法撤回。", f, "setup-notice");
  const submit = node("button", "加入网络", f, "primary"); submit.type = "submit"; submit.disabled = !ready;
  f.onsubmit = event => {
    event.preventDefault(); submittingControl = submit; document.activeElement?.blur();
    send("join_network", {value: draft.code.trim(), nickname: draft.nickname.trim(), consent: true, use_current_account: true, discoverable: draft.discoverable, accepts_requests: draft.accepts});
  };
}
// Dialogs live outside the scrollable page so they stay in the visible viewport.
const modalRoot = document.createElement("div");
modalRoot.id = "modal-root"; modalRoot.className = "modal-root"; modalRoot.hidden = true;
document.body.append(modalRoot);
let modalKind = "", modalKey = "", modalOpener = null, modalOpenerInfo = null;
function modalBackground(disabled) {
  for (const region of [document.querySelector(".topbar"), document.querySelector("main"), $("nav")]) {
    region.inert = disabled;
  }
}
function closeModal(restoreFocus = true) {
  if (!modalKind) return;
  modalRoot.hidden = true; modalRoot.replaceChildren();
  modalBackground(false); modalKind = ""; modalKey = "";
  const opener = modalOpener, info = modalOpenerInfo; modalOpener = null; modalOpenerInfo = null;
  if (restoreFocus) requestAnimationFrame(() => {
    if (modalKind) return;
    let target = opener?.isConnected ? opener : null;
    if (!target && info?.label) {
      const scope = info.member ? document.querySelector('[data-member-code="' + CSS.escape(info.member) + '"]') : document.querySelector("main");
      target = Array.from((scope || document.querySelector("main")).querySelectorAll("button,summary")).find(el => el.textContent.trim() === info.label);
    }
    if (target) target.focus({preventScroll: true});
  });
}
function openModal(kind, key, title) {
  const same = modalKind === kind && modalKey === key;
  const previous = same && modalRoot.contains(document.activeElement) ? document.activeElement.textContent : "";
  if (!modalKind) {
    modalOpener = document.activeElement;
    modalOpenerInfo = {label: modalOpener?.textContent?.trim() || "", member: modalOpener?.closest?.("[data-member-code]")?.dataset.memberCode || ""};
  }
  modalKind = kind; modalKey = key;
  modalRoot.replaceChildren(); modalRoot.hidden = false;
  const dialog = node("section", null, modalRoot, "modal-dialog");
  dialog.id = kind === "admin" ? "admin-confirm" : "wizard-confirm";
  dialog.setAttribute("role", "dialog"); dialog.setAttribute("aria-modal", "true");
  dialog.setAttribute("aria-labelledby", "modal-title");
  const heading = node("h2", title, dialog); heading.id = "modal-title";
  dialog.dataset.previousFocus = previous;
  return dialog;
}
function finishModal(dialog, cancel) {
  const actions = node("div", null, dialog, "modal-actions");
  const controls = Array.from(dialog.children).filter(el => el.tagName === "BUTTON");
  for (const control of controls) if (control !== cancel) actions.append(control);
  if (cancel) actions.prepend(cancel);
  const status = node("p", $("status").hidden ? "" : $("status").textContent, dialog, "modal-status"); status.id = "modal-status"; status.setAttribute("role", "status"); status.hidden = !status.textContent;
  const focus = controls.find(el => el.textContent === dialog.dataset.previousFocus) || cancel || controls[0] || dialog;
  if (focus === dialog) dialog.tabIndex = -1;
  focus.focus({preventScroll: true}); modalBackground(true);
}
modalRoot.addEventListener("keydown", event => {
  const controls = Array.from(modalRoot.querySelectorAll("button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),[tabindex='0']")).filter(el => el.getClientRects().length);
  if (event.key === "Escape") { event.preventDefault(); modalRoot.querySelector(".modal-cancel")?.click(); }
  if (event.key !== "Tab" || !controls.length) return;
  const first = controls[0], last = controls[controls.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
});
function renderAdminConfirm() {
  if (!adminConfirm) { if (modalKind === "admin") closeModal(); return; }
  const key = adminConfirm.operation + ":" + JSON.stringify(adminConfirm.data);
  if (modalKind === "admin" && modalKey === key) return;
  const dialog = openModal("admin", key, "确认操作");
  node("p", adminConfirm.question, dialog);
  button(dialog, "确认", () => {
    if (!adminConfirm) return;
    const c = adminConfirm; adminConfirm = null; closeModal();
    send("admin_action", {operation: c.operation, data: {...c.data, confirmed: "yes"}});
  }, "danger");
  const cancel = button(dialog, "取消", () => { adminConfirm = null; closeModal(); }, "quiet modal-cancel");
  finishModal(dialog, cancel);
}

function recoveryForm(parent) {
  const f = node("form", null, parent);
  const codeLabel = node("label", "网络加入码", f);
  const code = node("input", null, codeLabel); code.type = "password"; code.autocomplete = "off"; code.maxLength = 200; code.required = true; code.value = recoveryDraft.joinCode;
  const nameLabel = node("label", "原来的昵称", f);
  const nickname = node("input", null, nameLabel); nickname.maxLength = 40; nickname.required = true; nickname.value = recoveryDraft.nickname;
  code.addEventListener("input", () => { recoveryDraft.joinCode = code.value; });
  nickname.addEventListener("input", () => { recoveryDraft.nickname = nickname.value; });
  const addressLabel = node("label", "上次使用的账号或好友链接", f);
  const oldAddress = node("input", null, addressLabel); oldAddress.type = "text"; oldAddress.autocomplete = "off"; oldAddress.autocapitalize = "none"; oldAddress.maxLength = 2000; oldAddress.required = true; oldAddress.value = recoveryDraft.oldAddress;
  oldAddress.addEventListener("input", () => { recoveryDraft.oldAddress = oldAddress.value; });
  const submit = node("button", "提交恢复申请", f, "primary"); submit.type = "submit";
  f.onsubmit = event => {
    event.preventDefault();
    const address = accountFromInput(oldAddress.value);
    if (!address) { note("请输入上次使用的 Delta Chat 账号或好友链接。"); oldAddress.focus(); return; }
    document.activeElement?.blur();
    send("recover_by_name", {join_code: code.value.trim(), nickname: nickname.value.trim(), old_address: address});
  };
}
function renderWizard() {
  const wizard = $("wizard"); wizard.replaceChildren(); const p = state.pending;
  wizard.className = "";
  if (joinDraft) { if (modalKind === "wizard") closeModal(); return; }
  const confirmations = ["send_request", "send_vcard_request", "accept_request", "leave", "delete", "rotate_code", "remove_member", "ban_member", "disable_network"];
  const modal = Boolean(p && confirmations.includes(p.action));
  if (!modal && modalKind === "wizard") closeModal();
  if (!p || ["guide_next", "guide_profile", "guide_choose_target", "guide_search", "guide_contact_choice", "guide_contact_manual", "guide_privacy_choice"].includes(p.action)) return;
  const setup = ["guide_join_code", "guide_nickname", "join"].includes(p.action);
  if (dismissedStep === p.token) { if (modalKind === "wizard") closeModal(); return; }
  const titles = {guide_join_code: "加入网络", guide_nickname: "怎么称呼你？", join: "完成设置", guide_recover_identity: "恢复原账号", send_request: "确认好友申请", send_vcard_request: "确认发送名片", accept_request: "确认交换", leave: "退出网络", delete: "删除个人数据", rotate_code: "更换加入码", remove_member: "移除成员", ban_member: "封禁成员", disable_network: "停用网络"};
  const w = modal ? openModal("wizard", p.action + ":" + p.token, titles[p.action] || "确认操作") : wizard;
  if (!modal) { w.className = setup ? "card onboarding" : "card confirmation"; node("h2", titles[p.action] || "完成当前操作", w); }
  const reply = (value, extra = {}) => send("reply", {step: p.token, value, ...extra});
  if (p.action === "guide_join_code") {
    form(w, "网络加入码", value => reply(value), {max: 200, placeholder: "粘贴加入码（不是网络名称）", button: "下一步"});
  } else if (p.action === "guide_nickname") {
    node("p", p.network_name || "", w, "muted");
    form(w, "昵称", value => reply(value), {max: 40, placeholder: "输入你希望大家看到的名字", button: "下一步"});
  } else if (p.action === "join") {
    node("p", [p.network_name, p.nickname].filter(Boolean).join(" · "), w, "muted");
    const address = currentAccount(w);
    const visible = localSwitch(w, "在好友列表展示我");
    const accepts = localSwitch(w, "接收好友申请");
    node("p", "自动交换名片默认开启，同网络成员可与你交换；可在隐私设置关闭。交付后无法撤回。", w, "setup-notice");
    const finish = button(w, "使用当前账号并完成", () => reply("1", {consent: true, use_current_account: true, discoverable: visible(), accepts_requests: accepts()}), "primary");
    finish.disabled = !address;
  } else if (["send_request", "accept_request"].includes(p.action)) {
    node("p", "本次分享：" + (p.contact || "请先刷新核对联系方式"), w);
    node("p", "分享给：" + (p.recipient ? p.recipient.nickname : "请先刷新核对对象"), w);
    if (p.action === "send_request" && p.reason) node("p", "联系理由：" + p.reason, w);
    node("p", "双方同意后交换联系方式。", w);
    if (p.action !== "send_request") node("p", "双方同意后，联系人名片会发送到 Delta Chat 聊天中。", w, "muted");
    button(w, p.action === "send_request" ? "确认发送申请" : "确认交换联系方式", () => reply("1"), "primary");
  } else if (p.action === "send_vcard_request") {
    node("p", "确认向对方发送你的联系人名片？", w);
    button(w, "确认发送", () => reply("1"), "primary");
  } else if (p.action === "guide_recover_identity") {
    recoveryForm(w);
  } else if (p.action === "guide_incoming_request") {
    node("p", "在“申请”页查看申请人、理由及申请类型，再选择同意、拒绝或屏蔽。", w);
    button(w, "查看申请", () => { tab = "申请"; render(); }, "primary");
  } else if (["leave", "delete", "rotate_code", "remove_member", "ban_member", "disable_network"].includes(p.action)) {
    node("p", p.action === "delete" ? "这会清除个人资料并使相关未完成申请失效。已交付的信息无法收回。" : p.action === "leave" ? "退出后将从当前网络移除，尚未完成的申请会失效。" : "请确认要执行此操作。", w);
    button(w, p.action === "delete" ? "再次确认删除我的数据" : "确认执行", () => reply("1"), "danger");
  } else {
    const labels = {guide_join_code: "管理员提供的加入码", guide_nickname: "你希望大家怎么称呼你？", guide_edit_nickname: "新的昵称", guide_edit_bio: "个人介绍", guide_accept_contact: "愿意分享的 Delta Chat 地址", guide_admin_create_code: "按机器人提示设置加入码", guide_admin_rotate_code: "新加入码"};
    form(w, labels[p.action] || "按机器人回执填写", value => reply(value), {max: 500});
  }
  const cancel = button(w, waiting ? "返回" : "取消", () => {
    if (waiting || preparing) { dismissedStep = p.token; closeModal(); render(); return; }
    setup ? send("cancel_join", {step: p.token}) : command("取消");
  }, modal ? "quiet modal-cancel wizard-back" : "quiet wizard-back");
  if (modal) finishModal(w, cancel);
}
function renderFind(page) {
  node("h1", "好友", page);
  const directory = state.directory;
  if (!directory) {
    node("div", "正在加载好友列表…", page, "empty muted");
    button(page, "重新加载", () => send("directory", {page: 0}), "secondary");
    return;
  }
  if (!directory.members.length) {
    node("div", "这一页还没有公开资料的成员。", page, "empty muted");
  }
  const list = node("div", null, page, "friends-list");
  for (const person of directory.members) {
    const card = node("article", null, list, "person"); node("span", person.nickname.slice(0, 1), card, "avatar");
    const content = node("div", null, card);
    node("h3", person.nickname, content); if (person.bio) node("p", person.bio, content, "muted");
    const blocked = (state.blocked_members || []).some(m => m.member_code === person.member_code);
    const exchange = button(content, blocked ? "已屏蔽" : "交换名片", () => {
      if (!state.profile?.share_contact) {
        tab = "我的"; render(); note("请先设置联系方式。");
      } else send("exchange_now", {value: person.member_code});
    }, "primary friend-exchange");
    exchange.setAttribute("aria-label", blocked ? person.nickname + "已屏蔽" : "与" + person.nickname + "交换名片");
    exchange.disabled = blocked;
  }
  const paging = node("div", null, page, "pagination");
  if (directory.page > 0) button(paging, "上一页", () => { selected = null; send("directory", {page: directory.page - 1}); });
  if (directory.page > 0 || directory.has_more) node("span", "第 " + (directory.page + 1) + " 页", paging, "muted");
  if (directory.has_more) button(paging, "下一页", () => { selected = null; send("directory", {page: directory.page + 1}); });
}
function unreadCount() { return notifications.filter(n => !readNotifications[n.id]).length; }
function renderNotifications(page) {
  node("h1", "消息", page);
  if (!notifications.length) {
    if (state.pending?.action !== "send_request") node("div", "暂时没有新消息", page, "empty muted");
    return;
  }
  const list = node("div", null, page, "notification-list");
  for (const n of [...notifications].reverse()) {
    const card = node("article", null, list, "notification-row");
    const icon = node("img", null, card, "notification-avatar"); icon.src = "icon.png"; icon.alt = "";
    const body = node("div", null, card, "notification-body");
    node("strong", "好友助手", body);
    if (n.at) node("time", n.at.replace("T", " ").slice(0, 16), body, "muted");
    node("p", visibleText(n.body), body, "notification-text");
  }
}
function renderRequests(page) {
  node("h1", "我的申请", page);
  const tabs = node("div", null, page, "segments");
  for (const f of ["待处理", "已发出", "历史"]) button(tabs, f, () => { filter = f; render(); }, filter === f ? "selected" : "quiet").setAttribute("aria-pressed", String(filter === f));
  const rows = state.requests.filter(r => filter === "待处理" ? r.status === "pending_target" && r.direction === "incoming" : filter === "已发出" ? ["pending_target", "processing"].includes(r.status) && r.direction === "outgoing" : !["pending_target", "processing"].includes(r.status));
  if (!rows.length) node("div", "这里暂时没有申请。收到申请后会显示在这里。", page, "empty muted");
  const names = {pending_target: "等待同意", processing: "正在交付", completed: "已交付", rejected: "未获同意", withdrawn: "已撤销", expired: "已过期", invalid: "已失效"};
  for (const r of rows) {
    const card = node("article", null, page, "card"); node("span", r.delivery_uncertain ? "正在核对交付" : names[r.status] || "已结束", card, "badge");
    node("h3", r.direction === "incoming" ? r.sender + " 想认识你" : "发给 " + r.recipient, card); node("p", r.reason || "对方希望与你建立联系", card);
    node("small", r.public_id + " · " + (r.request_kind === "vcard" ? "接收对方联系人名片" : "双方授权交换联系方式"), card, "muted");
    if (r.status === "pending_target") {
      const actions = node("div", null, card, "actions");
      if (r.direction === "incoming") {
        button(actions, r.request_kind === "vcard" ? "同意接收名片" : "查看分享并同意", () => command("同意申请", r.public_id), "primary");
        button(actions, "拒绝", () => command("拒绝申请", r.public_id)); button(actions, "屏蔽此人", () => command("屏蔽", r.sender_code), "quiet");
      } else button(actions, "撤销申请", () => command("撤销申请", r.public_id));
    }
    const targetCode = r.direction === "incoming" ? r.sender_code : r.recipient_code;
    if (targetCode) {
      const more = node("details", null, card, "request-more"); node("summary", "举报此人", more);
      form(more, "举报原因", value => command("举报", targetCode + " " + value), {max: 500, multiline: true, button: "提交举报"});
    }
  }
}
function renderMe(page) {
  const p = state.profile; node("h1", p?.nickname || "我的资料", page);
  if (p) {
    const profile = settingsDisclosure(page, "昵称与个人介绍");
    form(profile, "昵称", value => command("编辑昵称", value), {value: p.nickname, max: 40, button: "保存昵称"});
    form(profile, "简短介绍", value => command("编辑介绍", value), {value: p.bio, required: false, multiline: true, button: "保存介绍"});
    const contact = settingsDisclosure(page, "联系方式");
    const address = p.communication_address || state.communication_address || "";
    node("p", address ? "当前账号：" + address : "暂时无法读取当前账号。", contact, "muted");
    if (p.share_contact && p.share_contact !== address) node("p", "当前分享：" + p.share_contact, contact, "muted");
    button(contact, "使用当前账号", () => send("use_current_account"), "primary contact-current").disabled = !address;
    const customContact = node("details", null, contact, "contact-custom");
    node("summary", "更改联系方式", customContact);
    const contactInput = form(customContact, "好友链接或 Delta Chat 账号", value => {
      if (value.includes("://") || value.toLowerCase().includes("i.delta.chat/#")) {
        const account = accountFromInput(value);
        if (!account) { note("请检查好友链接格式。"); return; }
        send("command", {command: "设置联系方式", value: account, verify_current_account: true});
      } else command("设置联系方式", value);
    }, {value: p.share_contact || "", max: 2000, multiline: true, placeholder: "粘贴好友链接或输入 name@example.org", button: "保存联系方式", buttonClass: "secondary"});
    const contactHint = node("p", "", customContact, "muted contact-hint");
    const updateHint = () => { const value = contactInput.value.trim(), account = accountFromInput(value); contactHint.textContent = value && !account ? "请检查好友链接或账号格式。" : account && account !== value ? "识别到账号：" + account : ""; };
    contactInput.addEventListener("input", updateHint); updateHint();
    const settings = settingsDisclosure(page, "隐私设置");
    const autoRow = node("div", null, settings, "setting"); node("span", "自动交换名片", autoRow);
    const autoToggle = button(autoRow, "", () => send("auto_share", {enabled: !p.auto_share}), "ios-switch");
    autoToggle.setAttribute("role", "switch"); autoToggle.setAttribute("aria-label", "自动交换名片"); autoToggle.setAttribute("aria-checked", String(Boolean(p.auto_share))); node("span", null, autoToggle, "switch-knob");
    node("p", "开启后，同网络中也开启此功能的成员可直接与你交换名片；关闭后停止新的自动交换。", settings, "muted");
    for (const [title, enabled, on, off] of [["允许同网络成员查找我", p.discoverable, "公开资料", "隐藏资料"], ["接收联系申请", p.accepts_requests, "接收申请", "暂停申请"]]) {
      const row = node("div", null, settings, "setting"); node("span", title, row);
      const toggle = button(row, "", () => command(enabled ? off : on), "ios-switch");
      toggle.setAttribute("role", "switch");
      toggle.setAttribute("aria-label", title);
      toggle.setAttribute("aria-checked", String(Boolean(enabled)));
      node("span", null, toggle, "switch-knob");
    }
  }
  const networks = settingsDisclosure(page, "我的网络");
  for (const n of state.networks) button(networks, (n.current ? "当前 · " : "切换 · ") + n.name, () => command("切换网络", n.public_id));
  button(networks, "加入新网络", () => openJoin(), "primary local-navigation");
  if (state.networks.some(n => n.current)) button(networks, "退出网络", () => command("退出网络"), "danger");
  const safety = settingsDisclosure(page, "屏蔽、举报与账号");
  const blocked = state.blocked_members || [];
  if (!blocked.length) node("p", "暂无屏蔽的成员。", safety, "muted");
  for (const person of blocked) {
    const row = node("div", null, safety, "setting"); node("span", person.nickname, row);
    button(row, "解除屏蔽", () => command("解除屏蔽", person.member_code));
  }
  const directory = state.directory;
  if (p && directory) {
    const label = node("label", "选择好友", safety);
    const select = node("select", null, label, "safety-select");
    const empty = node("option", "请选择好友", select); empty.value = "";
    for (const person of directory.members) {
      const option = node("option", person.nickname + (person.bio ? " · " + person.bio.slice(0, 24) : ""), select);
      option.value = person.member_code;
    }
    const actions = node("div", null, safety, "actions");
    const block = button(actions, "屏蔽所选好友", () => { if (select.value) command("屏蔽", select.value); }, "danger");
    block.disabled = true;
    const reason = form(safety, "举报原因", value => {
      if (!select.value) { note("请先选择好友。"); return; }
      command("举报", select.value + " " + value);
    }, {max: 500, multiline: true, button: "举报所选好友"});
    const report = reason.closest("form").querySelector("button"); report.disabled = true;
    select.onchange = () => { block.disabled = report.disabled = !select.value; };
    const paging = node("div", null, safety, "pagination");
    if (directory.page > 0) button(paging, "上一页", () => send("directory", {page: directory.page - 1}));
    if (directory.page > 0 || directory.has_more) node("span", "第 " + (directory.page + 1) + " 页", paging, "muted");
    if (directory.has_more) button(paging, "下一页", () => send("directory", {page: directory.page + 1}));
  } else if (p) button(safety, "加载好友", () => send("directory", {page: 0}));
  button(safety, "恢复原账号", () => command("恢复账号")); button(safety, "删除我的数据", () => command("删除我的数据"), "danger");
  const info = settingsDisclosure(page, "隐私说明与使用帮助"); privacy(info);
  node("p", "仅公开资料的成员会出现在目录中。申请 7 天后过期；双方确认后交换联系方式，请自行核验身份。", info, "muted");
  node("p", "点击按钮即可操作，结果自动同步。不需要返回聊天发送指令，也不需要网页后台账号。首次获取或更新时才需发送“小程序”。", info, "muted");
  button(page, "管理网络 ›", () => {
    expandedSettings.clear(); adminView = true; adminConfirm = null; render(); scrollTop();
    // A complete snapshot is already available locally; opening the page does
    // not need another network round trip. A new instance needs a context.
    if (!state.admin?.unlocked || !state.admin?.system || (state.admin.networks?.length && state.admin.current == null)) send("admin_open");
  }, "admin-entry");
}
function adminOperation(operation, data = {}, question = "") {
  if (question) {
    adminConfirm = {operation, data, question};
    renderAdminConfirm(); return;
  }
  send("admin_action", {operation, data});
}
function revealOperation(selector, focus = true) {
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const region = document.querySelector(selector) || document.querySelector("#admin-section");
    if (!region) return;
    document.querySelectorAll(".operation-focus").forEach(el => el.classList.remove("operation-focus"));
    if (!region.classList.contains("admin-actions")) region.classList.add("operation-focus");
    if (focus) {
      const control = region.matches("button,summary") ? region : region.querySelector("button,summary");
      const target = control || region;
      if (!control) target.tabIndex = -1;
      target.focus({preventScroll: true});
    }
    const main = document.querySelector("main");
    main.scrollTo({top: main.scrollTop + region.getBoundingClientRect().top - main.getBoundingClientRect().top - 12, behavior: "auto"});
  }));
}
async function unlockAdmin(secret) {
  const c = state.admin?.challenge;
  if (adminUnlocking || !c || waiting || !/^[0-9a-f]{64}$/i.test(c.nonce) || !/^[0-9a-f]{32}$/i.test(c.salt) || c.iterations !== 250000) {
    note("验证信息已过期，请重新打开管理页面。"); return;
  }
  adminUnlocking = true;
  try {
    const salt = Uint8Array.from(c.salt.match(/../g), x => parseInt(x, 16));
    const raw = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), "PBKDF2", false, ["deriveKey"]);
    const key = await crypto.subtle.deriveKey({name: "PBKDF2", salt, iterations: c.iterations, hash: "SHA-256"}, raw, {name: "HMAC", hash: "SHA-256", length: 256}, false, ["sign"]);
    const signature = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode("contact-admin-v1:" + c.nonce + ":" + window.CONTACT_CONFIG.token));
    const proof = Array.from(new Uint8Array(signature), b => b.toString(16).padStart(2, "0")).join("");
    await send("admin_unlock", {proof});
  } catch { note("无法验证密钥，请更新 Delta Chat 后重试。"); }
  finally { adminUnlocking = false; }
}
function adminButtonGroup(parent, entries) {
  const group = node("div", null, parent, "admin-actions");
  for (const [label, fn, style] of entries) button(group, label, fn, style || "secondary");
}
function renderAdmin(page) {
  const top = node("div", null, page, "admin-top");
  button(top, "‹ 返回", () => { adminView = false; adminConfirm = null; render(); }, "quiet admin-back");
  node("h1", "网络管理", top);
  if (state.admin?.unlocked) button(top, "退出授权", () => { adminConfirm = null; send("admin_lock"); }, "quiet admin-lock");
  const data = state.admin;
  if (!data) { node("div", "正在打开管理工具…", page, "empty muted"); return; }
  if (!data.unlocked) {
    const card = node("section", null, page, "card admin-unlock");
    node("h2", "验证管理员密钥", card);
    node("p", "验证后为当前账号保留管理权限。", card, "muted");
    if (state.receipt && !state.receipt.ok && state.messages.length) node("p", visibleText(state.messages[state.messages.length - 1]), card, "admin-error");
    if (!data.challenge) {
      node("p", "请获取验证信息后输入密钥。", card, "muted");
      button(card, "获取验证信息", () => send("admin_open"), "primary");
    } else form(card, "管理员密钥", unlockAdmin, {type: "password", autocomplete: "off", clearOnSubmit: true, max: 128, button: "解锁管理"});
    return;
  }
  const networks = data.networks || [], current = networks.find(n => n.id === data.current);
  if (networks.length) {
    const chooser = node("label", null, page, "admin-network-label");
    node("span", "当前网络", chooser);
    const select = node("select", null, chooser, "admin-select");
    for (const n of networks) { const option = node("option", n.name, select); option.value = String(n.id); option.selected = n.id === data.current; }
    select.onchange = () => { adminConfirm = null; send("admin_select", {admin_network: Number(select.value), page: 0}); };
  }
  const sections = data.system ? ["网络", "成员", "审核", "记录", "系统"] : ["网络", "成员", "审核", "记录"];
  if (!sections.includes(adminSection)) adminSection = "网络";
  const tabs = node("div", null, page, "admin-tabs");
  for (const section of sections) button(tabs, section, () => { adminSection = section; adminConfirm = null; render(); }, section === adminSection ? "selected" : "quiet");
  if (adminResultKey && state.receipt?.request_id === adminResultKey && state.messages.length) {
    const result = node("div", null, page, "admin-result");
    node("p", visibleText(state.messages[state.messages.length - 1]), result);
    if (adminResultPersistent) button(result, "关闭", () => { adminResultKey = null; result.remove(); }, "quiet");
  }
  page = node("section", null, page); page.id = "admin-section";
  if (!current && adminSection !== "系统") { node("div", "暂无可管理的网络。", page, "empty muted"); return; }
  if (adminSection === "网络") {
    const card = node("section", null, page, "card network-settings");
    const heading = node("div", null, card, "network-heading");
    const identity = node("div", null, heading);
    node("h2", current.name, identity);
    node("p", current.member_count + " 位成员" + (current.status === "active" ? "" : " · 已停用"), identity, "muted");
    const joined = state.networks.find(n => n.public_id === current.public_id);
    if (current.status === "active") {
      if (joined) button(heading, "进入网络", () => command("切换网络", current.public_id), "quiet");
      else if (current.join_enabled) button(heading, "加入网络", () => openJoin(), "quiet local-navigation");
    }
    const actionRow = (label, callback, value = "") => {
      const control = button(card, "", callback, "quiet network-setting-row");
      node("span", label, control);
      const end = node("span", null, control, "network-setting-end");
      if (value) node("span", value, end, "muted");
      const arrow = node("span", "›", end, "network-chevron"); arrow.setAttribute("aria-hidden", "true");
      return control;
    };
    if (data.system) actionRow("网络名称", () => {
      const dialog = openModal("network-name", String(current.id), "修改网络名称");
      const input = form(dialog, "网络名称", value => { closeModal(); adminOperation("rename", {name: value}); }, {value: current.name, max: 60, button: "保存"});
      const cancel = button(dialog, "取消", () => closeModal(), "quiet modal-cancel");
      finishModal(dialog, cancel); input.focus({preventScroll: true});
    }, current.name);
    const row = node("div", null, card, "setting network-setting-row"); node("span", "允许新成员加入", row);
    const toggle = button(row, "", () => adminOperation(current.join_enabled ? "join_off" : "join_on"), "ios-switch");
    toggle.setAttribute("role", "switch"); toggle.setAttribute("aria-label", "允许新成员加入"); toggle.setAttribute("aria-checked", String(Boolean(current.join_enabled))); node("span", null, toggle, "switch-knob");
    const codeRow = node("div", null, card, "network-setting-row");
    node("span", "加入码", codeRow);
    node("span", data.join_code || "暂不可用", codeRow, "network-join-code");
    actionRow("更换加入码", () => {
      const dialog = openModal("network-code", String(current.id), "更换加入码");
      node("p", "保存后旧加入码立即失效，现有成员不受影响。", dialog, "muted");
      const input = form(dialog, "新加入码", value => {
        if (value.length < 6 || value.length > 200 || /\s/.test(value)) { note("加入码需为 6–200 个字符，不能含空格。"); return; }
        closeModal(); adminOperation("rotate", {code: value});
      }, {max: 200, autocomplete: "off", placeholder: "输入自定义加入码", button: "保存加入码"});
      input.minLength = 6; input.autocapitalize = "none"; input.spellcheck = false; input.setAttribute("autocorrect", "off");
      const cancel = button(dialog, "取消", () => closeModal(), "quiet modal-cancel");
      finishModal(dialog, cancel); input.focus({preventScroll: true});
    });
    if (data.system) button(page, current.status === "active" ? "停用网络" : "恢复网络", () => adminOperation(current.status === "active" ? "disable" : "enable", {}, current.status === "active" ? "停用后，该网络暂时无法使用。" : "确定恢复该网络？"), "quiet network-status-action" + (current.status === "active" ? " network-stop" : ""));
  } else if (adminSection === "成员") {
    const members = data.members || [];
    if (!members.length) node("div", "没有成员。", page, "empty muted");
    for (const m of members) {
      const detail = disclosure(page, m.nickname + " · " + (m.is_self ? "本人" : m.discoverable ? "首页可见" : "资料隐藏"));
      detail.dataset.memberCode = m.member_code;
      detail.open = expandedMembers.has(m.member_code);
      detail.addEventListener("toggle", () => { if (detail.isConnected) { if (detail.open) expandedMembers.add(m.member_code); else expandedMembers.delete(m.member_code); } });
      detail.querySelector("summary").addEventListener("click", () => requestAnimationFrame(() => {
        if (detail.open) { expandedMembers.add(m.member_code); if (detail.querySelector(".admin-actions button")) revealOperation('[data-member-code="' + CSS.escape(m.member_code) + '"] .admin-actions'); }
        else expandedMembers.delete(m.member_code);
      }));
      node("p", (m.is_system_admin || m.key_admin) ? "系统管理员" : (m.role === "network_admin" ? "网络管理员" : "成员") + (m.banned ? " · 已封禁" : m.status !== "active" ? " · 已退出或移除" : ""), detail, "muted");
      if (m.is_system_admin) continue;
      const actions = [];
      if (m.banned) actions.push(["解除封禁", () => adminOperation("unban", {member_code: m.member_code}, "解除 " + m.nickname + " 的封禁？")]);
      if (m.status === "active" && m.role === "member") {
        actions.push(["移除成员", () => adminOperation("remove", {member_code: m.member_code}, "确定移除 " + m.nickname + "？") , "danger"]);
      }
      if (m.status === "active" && data.system) actions.push([m.role === "network_admin" ? "撤销管理员" : "任命管理员", () => adminOperation(m.role === "network_admin" ? "revoke" : "appoint", {member_code: m.member_code}, "确定更改 " + m.nickname + " 的管理员权限？")]);
      adminButtonGroup(detail, actions);
      if (m.status === "active" && m.role === "network_admin" && data.system) node("p", "移除前，请先撤销管理员权限。", detail, "muted");
    }
    const paging = node("div", null, page, "pagination");
    if (data.page > 0) button(paging, "上一页", () => send("admin_select", {admin_network: current.id, page: data.page - 1}));
    if (data.page > 0 || data.has_more) node("span", "第 " + (data.page + 1) + " 页", paging, "muted");
    if (data.has_more) button(paging, "下一页", () => send("admin_select", {admin_network: current.id, page: data.page + 1}));
    if (data.bans?.length) {
      const banned = disclosure(page, "封禁名单");
      for (const m of data.bans) {
        const row = node("div", null, banned, "setting"); node("span", m.nickname, row);
        button(row, "解除封禁", () => adminOperation("unban", {member_code: m.member_code}, "解除 " + m.nickname + " 的封禁？"));
      }
    }
  } else if (adminSection === "审核") {
    if ((data.deliveries || []).length) {
      node("h2", "待核查名片交付", page);
      for (const d of data.deliveries) {
        const card = node("section", null, page, "card"); node("h3", d.recipient + " · " + d.public_id, card);
        node("p", "系统正在核对已发送记录，不会自动重复发送。", card);
        button(card, "刷新核对结果", () => send("refresh"));
        button(card, "结束此交换", () => adminOperation("delivery_close", {outbox_id: String(d.id)}, "请先让双方检查聊天中的名片。结束后不会重发；确实未收到时可重新发起交换。"), "danger");
      }
    }
    node("h2", "待处理举报", page);
    const reports = (data.reports || []).filter(r => r.status === "open");
    if (!reports.length) node("p", "暂无待处理举报。", page, "muted");
    for (const r of reports) {
      const card = node("section", null, page, "card"); node("h3", r.target + " · " + r.public_id, card);
      node("p", "举报人：" + r.reporter, card, "muted"); node("p", r.reason, card);
      form(card, "处理结果", value => adminOperation("resolve_report", {report_id: r.public_id, resolution: value}), {max: 300, button: "完成处理"});
    }
    node("h2", "账号恢复", page);
    if (!(data.recoveries || []).length) node("p", "暂无待审核申请。", page, "muted");
    for (const r of data.recoveries || []) {
      const card = node("section", null, page, "card"); node("h3", r.nickname, card); node("p", r.public_id, card, "muted");
      adminButtonGroup(card, [["批准恢复", () => adminOperation("approve_recovery", {recovery_id: r.public_id}, "确认已核实 " + r.nickname + " 的身份？"), "primary"], ["拒绝", () => adminOperation("reject_recovery", {recovery_id: r.public_id}, "确定拒绝该恢复申请？"), "danger"]]);
    }
  } else if (adminSection === "记录") {
    const names = {"web:view_join_code":"查看加入码", "create_network":"创建网络", "rename_network":"修改网络名称", "set_role":"更改管理员", "remove_member":"移除成员", "ban_member":"封禁成员", "unban_member":"解除封禁", "rotate_join_code":"更换加入码", "enable_network":"恢复网络", "disable_network":"停用网络", "approve_account_recovery":"批准账号恢复", "reject_account_recovery":"拒绝账号恢复", "merge_duplicate_identity":"自动合并账号", "close_uncertain_delivery":"结束待核查交付"};
    if (!(data.audit || []).length) node("div", "暂无管理记录。", page, "empty muted");
    for (const a of data.audit || []) { const card = node("div", null, page, "card admin-audit"); node("strong", names[a.action] || "管理操作", card); if (a.target_ref && !/^M[A-HJ-NP-Z2-9]{6}$/.test(a.target_ref)) node("p", a.target_ref, card); node("small", (a.created_at || "").replace("T", " ").slice(0, 16), card, "muted"); }
  } else if (adminSection === "系统") {
    const card = node("section", null, page, "card"); node("h2", "创建网络", card);
    form(card, "网络名称", value => adminOperation("create", {name: value}), {max: 60, button: "创建网络"});
    node("p", "系统会生成加入码，请妥善保管。", card, "muted");
  }
}
function render() {
  document.body.classList.toggle("admin-mode", tab === "我的" && adminView);
  const current = state.networks.find(n => n.current); $("network").textContent = state.pending?.network_name ? "正在加入 · " + state.pending.network_name : current?.name || "好友助手";
  const page = $("page"), wizard = $("wizard");
  if (wizard.parentElement === page) page.parentElement.insertBefore(wizard, page);
  renderWizard(); page.replaceChildren();
  const setup = !joinDraft && bound && ready && dismissedStep !== state.pending?.token && ["guide_join_code", "guide_nickname", "join"].includes(state.pending?.action);
  page.hidden = setup;
  if (joinDraft) {
    renderJoin(page);
  } else if (setup) {
    // Only the active onboarding step is shown.
  } else if (!bound) {
    node("h1", "欢迎使用好友助手", page);
    const card = node("section", null, page, "card"); node("h2", "先连接好友助手", card);
    node("p", "点击下方按钮，把“小程序”发送给好友助手，然后打开收到的应用。", card);
    button(card, "向好友助手获取我的应用", () => send("refresh"), "primary"); privacy(card);
  } else if (!ready) {
    node("h1", "正在读取你的网络", page); node("p", "等待机器人状态。若一直未出现，回到私聊发送“小程序”。", page, "empty muted");
  } else if (!state.profile && !["我的", "消息"].includes(tab)) {
    node("h1", "加入你的网络", page);
    const card = node("section", null, page, "card onboarding");
    button(card, "加入网络", () => openJoin(), "primary local-navigation");
    node("p", "加入码请勿公开传播。", card, "setup-notice");
  } else if (tab === "我的" && adminView) renderAdmin(page);
  else if (state.profile && !state.profile.share_contact && tab === "好友") {
    node("h1", "完成账号设置", page);
    const card = node("section", null, page, "card onboarding");
    const address = currentAccount(card);
    node("p", "账号仅在双方同意后分享。", card, "setup-notice");
    button(card, "使用当前账号", () => send("use_current_account"), "primary").disabled = !address;
  }
  else if (tab === "好友") renderFind(page);
  else if (tab === "申请") renderRequests(page);
  else if (tab === "消息") {
    renderNotifications(page);

  } else renderMe(page);
  renderNav();
  syncOperationControls();
  if (modalKind !== "wizard") {
    if (tab === "我的" && adminView && state.admin?.unlocked) renderAdminConfirm();
    else if (modalKind === "admin") { adminConfirm = null; closeModal(); }
  }
}
function renderNav() {
  $("nav").replaceChildren();
  const icons = {"好友": "M4 20v-1a6 6 0 0 1 12 0v1M10 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8ZM18 8h5M20.5 5.5v5", "申请": "M4 5h16v14H4zM7 9h10M7 13h7", "消息": "M4 6a3 3 0 0 1 3-3h10a3 3 0 0 1 3 3v8a3 3 0 0 1-3 3H10l-5 4v-4a3 3 0 0 1-1-3z", "我的": "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8ZM4 21a8 8 0 0 1 16 0"};
  for (const name of ["好友", "申请", "消息", "我的"]) {
    const b = button($("nav"), "", () => {
      if (tab === "我的" && name !== "我的") expandedSettings.clear();
      if (joinDraft) joinDraft = null;
      tab = name;
      if (name === "消息") { notifications.forEach(n => { readNotifications[n.id] = true; }); try { localStorage.setItem("friend-notifications-read", JSON.stringify(readNotifications)); } catch {} }
      render(); scrollTop();
    }, tab === name ? "active" : "");
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path"); path.setAttribute("d", icons[name]); svg.append(path); b.append(svg);
    if (name === "消息" && unreadCount()) node("span", String(Math.min(99, unreadCount())), b, "nav-badge");
    node("span", name, b); if (tab === name) b.setAttribute("aria-current", "page");
  }
}
function renderFromRemote(force) {
  const active = document.activeElement;
  if (!force && active && ["INPUT", "TEXTAREA"].includes(active.tagName) && active.closest("form")) {
    // Preserve the whole form until the user explicitly submits or leaves it.
    deferredRemoteRender = true;
    renderNav();
    return;
  }
  deferredRemoteRender = false;
  render();
}
$("refresh").onclick = () => send("refresh");
let viewportFrame;
let keyboardScroll = null;
let restingHeight = document.documentElement.clientHeight || window.innerHeight;
let viewportWidth = window.innerWidth;
const editableFocused = () => document.activeElement?.matches("input,textarea,select,[contenteditable=true]");
function resetPageOffset() {
  window.scrollTo(0, 0);
  document.documentElement.scrollTop = 0;
  document.body.scrollTop = 0;
}
function keepFocusedControlVisible(viewport) {
  const active = document.activeElement, main = document.querySelector("main");
  if (!active || !main.contains(active) || !active.matches("input,textarea,select,[contenteditable=true]")) return;
  const box = main.getBoundingClientRect();
  const top = Math.max(box.top, viewport?.offsetTop || 0);
  const bottom = Math.min(box.bottom, (viewport?.offsetTop || 0) + (viewport?.height || window.innerHeight));
  const form = active.closest("form"), visibleHeight = Math.max(0, bottom - top);
  const target = form && form.getBoundingClientRect().height < visibleHeight - 24 ? form : active;
  const rect = target.getBoundingClientRect();
  if (rect.bottom > bottom - 12) main.scrollTop += rect.bottom - bottom + 12;
  else if (rect.top < top + 12) main.scrollTop -= top + 12 - rect.top;
}
function fitViewport() {
  cancelAnimationFrame(viewportFrame);
  viewportFrame = requestAnimationFrame(() => {
    const viewport = window.visualViewport, main = document.querySelector("main");
    if (Math.abs(window.innerWidth - viewportWidth) > 80) {
      viewportWidth = window.innerWidth;
      restingHeight = document.documentElement.clientHeight || window.innerHeight;
      keyboardScroll = null;
    }
    if (viewport && Math.abs(viewport.scale - 1) > .01) return;
    const height = viewport?.height || window.innerHeight;
    const focused = editableFocused();
    const keyboardOpen = focused && height < restingHeight - 100;
    if (keyboardOpen) {
      if (keyboardScroll === null) keyboardScroll = main.scrollTop;
      requestAnimationFrame(() => keepFocusedControlVisible(viewport));
    } else if (!focused || height >= restingHeight - 20) {
      if (keyboardScroll !== null) { main.scrollTop = keyboardScroll; keyboardScroll = null; }
      if (!focused) {
        resetPageOffset();
        restingHeight = document.documentElement.clientHeight || window.innerHeight;
      }
    }
  });
}
window.visualViewport?.addEventListener("resize", fitViewport);
window.visualViewport?.addEventListener("scroll", fitViewport);
window.addEventListener("resize", fitViewport);
document.addEventListener("focusin", fitViewport);
document.addEventListener("focusout", () => {
  fitViewport();
  [100, 300, 600, 1000].forEach(ms => setTimeout(() => {
    if (!editableFocused()) { resetPageOffset(); fitViewport(); }
  }, ms));
});
fitViewport();
if (waiting) markUnknown(); else render();
if (window.XdcTransportListen && bound) {
  Promise.resolve(window.XdcTransportListen(update => {
    const p = update.payload;
    if (p?.type === "contact-notification" && typeof p.id === "string" && typeof p.body === "string") {
      if (!notifications.some(n => n.id === p.id)) notifications = [...notifications, {id: p.id, body: p.body, at: p.at || ""}].slice(-200);
      if (tab === "消息") { readNotifications[p.id] = true; try { localStorage.setItem("friend-notifications-read", JSON.stringify(readNotifications)); } catch {} }
      if (tab === "消息") render(); else renderNav();
      return;
    }
    if (!p || p.type !== "contact-state" || !Array.isArray(p.networks) || !Array.isArray(p.messages)) return;
    if (Number.isInteger(update.serial)) {
      if (update.serial <= lastStateSerial) return;
      lastStateSerial = update.serial;
    }
    const firstState = !ready;
    const settled = (p.recent_receipts || []).filter(receipt => unresolved.some(item => item.id === receipt.request_id));
    if (settled.length) {
      unresolved = unresolved.filter(item => !settled.some(receipt => receipt.request_id === item.id));
      rememberUnresolved();
      note(settled.every(receipt => receipt.ok) ? "之前的操作已完成。" : "之前的操作未完成。");
    }
    const before = JSON.stringify({...state, at: null, receipt: null});
    const previousStep = state.pending?.action;
    const operationStep = waiting?.step || previousStep;
    const changed = p.current_network !== state.current_network || !p.profile;
    const deletedProfile = Boolean(state.profile) && !p.profile;
    const unlockSucceeded = waiting && waiting.kind === "admin_unlock" && p.receipt?.request_id === waiting.id && p.receipt.ok;
    state = {...p, requests: p.requests || [], choices: p.choices || [], messages: unlockSucceeded ? [] : p.messages.length || changed ? p.messages : state.messages};
    ready = true; if (changed) { selected = null; expandedMembers.clear(); expandedSettings.clear(); adminConfirm = null; if (deletedProfile) notifications = []; }
    const joinResult = joinDraft?.requestId && (p.recent_receipts || []).find(receipt => receipt.request_id === joinDraft.requestId);
    if (joinResult) {
      if (joinResult.ok) { joinDraft = null; tab = "好友"; adminView = false; scrollTop(); note("已加入网络。"); }
      else { joinDraft.requestId = null; note(joinResult.error || "加入未完成，请检查加入码。"); }
    }
    const receivedReceipt = Boolean(waiting && p.receipt?.request_id === waiting.id);
    const adminReceipt = receivedReceipt && ["admin_open", "admin_unlock", "admin_action", "admin_select", "admin_lock"].includes(waiting.kind) ? waiting.kind : null;
    if (receivedReceipt) {
      if (adminReceipt === "admin_action" && p.messages.length) {
        adminResultKey = p.receipt.request_id;
        if (p.receipt.ok && waiting.operation === "create") adminSection = "网络";
        adminResultPersistent = p.receipt.ok && ["create", "view_code", "rotate"].includes(waiting.operation);
        clearTimeout(adminResultTimer);
        if (!adminResultPersistent) adminResultTimer = setTimeout(() => {
          adminResultKey = null;
          document.querySelector(".admin-result")?.remove();
        }, 3000);
      }
      const showConfirmation = waiting.openMessages && p.receipt.ok && p.pending?.action === "send_request";
      const recoverySubmitted = waiting.kind === "recover_by_name" && p.receipt.ok;
      const joiningStep = ["guide_join_code", "guide_nickname", "join"].includes(operationStep);
      const cancelledJoin = p.receipt.ok && joiningStep && (waiting.kind === "cancel_join" || (waiting.kind === "command" && waiting.command === "取消"));
      const completedJoin = p.receipt.ok && (waiting.kind === "join_network" || (waiting.kind === "reply" && operationStep === "join") || ((waiting.kind === "reply" || waiting.kind === "join") && changed && p.profile && (operationStep === "guide_join_code" || waiting.kind === "join")) || (waiting.kind === "join_managed" && !p.pending && p.profile));
      if (p.receipt.ok && waiting.kind === "join_managed") { adminView = false; scrollTop(); }
      if (p.receipt.ok && waiting.kind === "command" && waiting.command === "切换网络") { tab = "好友"; adminView = false; scrollTop(); }
      if (cancelledJoin) { adminView = false; tab = state.profile ? "我的" : "好友"; scrollTop(); }
      if (completedJoin && (waiting.kind !== "join_network" || joinResult)) { tab = "好友"; adminView = false; scrollTop(); }
      waiting = null; operationFeedback(null); rememberPending(); clearTimeout(waitTimer);
      if (showConfirmation) {
        tab = "消息"; selected = null;
        notifications.forEach(n => { readNotifications[n.id] = true; });
        try { localStorage.setItem("friend-notifications-read", JSON.stringify(readNotifications)); } catch {}
        scrollTop();
      }
      const error = (p.recent_receipts || []).find(receipt => receipt.request_id === p.receipt.request_id)?.error;
      note(p.receipt.ok ? (recoverySubmitted ? "恢复申请已提交，请联系管理员核实。" : cancelledJoin ? "已取消加入。" : completedJoin ? "已进入所选网络。" : "操作完成。") : (error || p.messages[p.messages.length - 1] || "操作未完成，请重试。"));
    } else if (!waiting && firstState && !settled.length) note("");
    if (receivedReceipt || joinResult || before !== JSON.stringify({...state, at: null, receipt: null})) renderFromRemote(receivedReceipt || Boolean(joinResult) || changed);
    if (adminReceipt && adminView) {
      if (!p.receipt.ok) revealOperation(".admin-error, #status");
      else if (!state.admin?.unlocked) revealOperation(".admin-unlock", false);
      else if (adminReceipt === "admin_action") revealOperation(".admin-result", false);
    }
    if (previousStep === "guide_recover_identity" && state.pending?.action !== previousStep) recoveryDraft = {joinCode: "", nickname: "", oldAddress: ""};
    if (receivedReceipt && previousStep !== state.pending?.action && ["guide_join_code", "guide_nickname", "join"].includes(previousStep)) scrollTop();

  }, 0)).catch(() => note("状态读取失败。请重新打开，或向机器人发送“小程序”。"));
} else note(window.webxdc ? "请先向好友助手获取应用。" : "浏览器仅可预览界面；请在 Delta Chat 中打开 .xdc 文件。");
