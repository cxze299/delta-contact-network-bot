"use strict";
// Identity and permissions come from Core and the server, never selfAddr.
const $ = id => document.getElementById(id);
let state = {networks: [], pending: null, messages: [], requests: [], choices: [], profile: null, current_network: null};
let tab = "好友", filter = "待处理", preparing = false, ready = false, selected = null;
let autoNetwork = null, registrationAttempted = false;
let deferredRemoteRender = false;
let adminView = false, adminSection = "网络", adminConfirm = null, adminUnlocking = false;
const expandedMembers = new Set();
let notifications = [], readNotifications = {};
try { readNotifications = JSON.parse(localStorage.getItem("friend-notifications-read") || "{}"); } catch { /* Storage is optional. */ }
let waiting = null, waitTimer;
try { waiting = JSON.parse(localStorage.getItem("friend-pending-v2") || "null"); } catch { /* Storage is optional. */ }
const bound = Boolean(window.CONTACT_CONFIG?.token);
function node(tag, text, parent, cls = "") {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = text;
  e.className = cls; if (parent) parent.append(e); return e;
}
function button(parent, text, fn, cls = "secondary") {
  const e = node("button", text, parent, cls); e.type = "button"; e.onclick = fn; return e;
}
function note(text) { $("status").textContent = text; $("status").hidden = !text; }
function rememberPending() {
  try {
    if (waiting) localStorage.setItem("friend-pending-v2", JSON.stringify(waiting));
    else localStorage.removeItem("friend-pending-v2");
  } catch { /* Transport still works when local storage is unavailable. */ }
}
function watchPending() {
  clearTimeout(waitTimer);
  $("retry").hidden = true;
  waitTimer = setTimeout(() => {
    if (!waiting) return;
    note("仍在等待机器人响应。请检查网络；重试会使用同一请求，不会重复执行。");
    $("retry").hidden = false;
  }, 30000);
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
  try {
    await window.webxdc.sendUpdate({payload: waiting.payload}, "");
    if (waiting) { note("正在同步，请稍候…"); watchPending(); }
  } catch { note("暂时无法同步，请稍后重试。"); $("retry").hidden = false; }
}
async function send(action, extra = {}) {
  if (!window.webxdc) { note("请在 Delta Chat 内打开好友助手小应用。"); return; }
  if (preparing) return;
  if (!bound) {
    try { await window.webxdc.sendToChat({text: "小程序"}); }
    catch { note("请在好友助手私聊发送一次“小程序”。"); }
    return;
  }
  if (waiting) { note("上一项操作正在同步，请稍候。等待过久可以重试同一请求。"); return; }
  if (bound && !ready) { note("还未收到机器人状态，请稍等；长期无响应可发送“小程序”获取新版。"); return; }
  if (!window.webxdc.sendUpdate || !crypto.subtle) { note("当前客户端缺少所需能力，请更新 Delta Chat。"); return; }
  preparing = true;
  const id = Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2, "0")).join("");
  note("正在提交…");
  try {
    const payload = await seal({token: window.CONTACT_CONFIG.token, id, action, network: state.current_network, notification_version: 1, ...extra});
    waiting = {id, payload, kind: action, openMessages: action === "command" && extra.command === "申请"}; rememberPending();
    await resend();
  } catch { note("提交失败，请稍后重试。"); }
  finally { preparing = false; }
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
  const b = node("button", options.button || "继续", f, "primary"); b.type = "submit";
  f.onsubmit = e => { e.preventDefault(); const value = input.value.trim(); if (options.clearOnSubmit) input.value = ""; input.blur(); submit(value); }; return input;
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
function localSwitch(parent, text) {
  const row = node("div", null, parent, "setting"); node("span", text, row);
  let enabled = true;
  const toggle = button(row, "", () => { enabled = !enabled; toggle.setAttribute("aria-checked", String(enabled)); }, "ios-switch");
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
function privacy(parent) {
  node("p", "机器人服务端可以处理你发送的数据。只收集昵称、介绍及必要账号标识；目录不展示联系方式。", parent, "muted");
  node("p", "联系方式仅按你的授权分享；交付后无法从接收方设备收回。小应用更新留存在聊天历史中，删除服务器数据不会自动删除设备上的历史，请自行删除旧的小应用消息。", parent, "muted");
}
function renderWizard() {
  const w = $("wizard"); w.replaceChildren(); const p = state.pending;
  w.className = "";
  if (!p || ["guide_next", "guide_profile", "guide_choose_target", "guide_search", "guide_contact_choice", "guide_contact_manual", "guide_privacy_choice"].includes(p.action)) return;
  const setup = ["guide_join_code", "guide_nickname", "join"].includes(p.action);
  w.className = setup ? "card onboarding" : "card confirmation";
  const titles = {guide_join_code: "加入网络", guide_nickname: "怎么称呼你？", join: "完成设置", send_request: "确认好友申请", accept_request: "确认交换", leave: "退出网络", delete: "删除个人数据"};
  node("h2", titles[p.action] || "完成当前操作", w);
  const reply = (value, extra = {}) => send("reply", {step: p.token, value, ...extra});
  if (p.action === "guide_join_code") {
    form(w, "网络加入码", value => reply(value), {max: 200, placeholder: "输入管理员提供的加入码", button: "下一步"});
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
    node("p", "分享给：" + (p.recipient ? p.recipient.nickname + "（" + p.recipient.member_code + "）" : "请先刷新核对对象"), w);
    if (p.action === "send_request" && p.reason) node("p", "联系理由：" + p.reason, w);
    node("p", "双方同意后交换联系方式。", w);
    if (p.action !== "send_request") node("p", "双方同意后，联系人名片会发送到 Delta Chat 聊天中。", w, "muted");
    button(w, p.action === "send_request" ? "确认发送申请" : "确认交换联系方式", () => reply("1"), "primary");
  } else if (p.action === "guide_incoming_request") {
    node("p", "在“申请”页查看申请人、理由及申请类型，再选择同意、拒绝或屏蔽。", w);
    button(w, "查看申请", () => { tab = "申请"; render(); }, "primary");
  } else if (["leave", "delete", "rotate_code", "remove_member", "ban_member", "disable_network"].includes(p.action)) {
    node("p", p.action === "delete" ? "这会清除个人资料并使相关未完成申请失效。已交付的信息无法收回。" : p.action === "leave" ? "退出后将从当前网络移除，尚未完成的申请会失效。" : "请确认要执行此操作。", w);
    button(w, p.action === "delete" ? "再次确认删除我的数据" : "确认执行", () => reply("1"), "danger");
  } else {
    const labels = {guide_join_code: "管理员提供的加入码", guide_nickname: "你希望大家怎么称呼你？", guide_edit_nickname: "新的昵称", guide_edit_bio: "个人介绍", guide_accept_contact: "愿意分享的 Delta Chat 地址", guide_admin_create_code: "按机器人提示设置加入码", guide_admin_rotate_code: "新加入码", guide_recover_identity: "网络编号和原成员编号"};
    form(w, labels[p.action] || "按机器人回执填写", value => reply(value), {max: 500});
  }
  button(w, "取消", () => command("取消"), "quiet");
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
    node("h3", person.nickname, content); node("small", person.member_code, content, "muted"); if (person.bio) node("p", person.bio, content, "muted");
    button(content, selected === person.member_code ? "收起" : "添加", () => { selected = selected === person.member_code ? null : person.member_code; render(); }, "friend-add");
    if (selected === person.member_code) {
      if (!state.profile?.share_contact) {
        node("p", "申请前，请先确认你愿意分享的联系方式。", content);
        button(content, "去设置联系方式", () => { tab = "我的"; render(); }, "primary");
      } else {
        button(content, "交换名片", () => send("exchange_now", {value: person.member_code}), "primary");
      }
    }
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
    node("p", n.body, body, "notification-text");
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
    const card = node("article", null, page, "card"); node("span", names[r.status] || "已结束", card, "badge");
    node("h3", r.direction === "incoming" ? r.sender + " 想认识你" : "发给 " + r.recipient, card); node("p", r.reason || "对方希望与你建立联系", card);
    node("small", r.public_id + " · " + (r.request_kind === "vcard" ? "接收对方联系人名片" : "双方授权交换联系方式"), card, "muted");
    if (r.status === "pending_target") {
      const actions = node("div", null, card, "actions");
      if (r.direction === "incoming") {
        button(actions, r.request_kind === "vcard" ? "同意接收名片" : "查看分享并同意", () => command("同意申请", r.public_id), "primary");
        button(actions, "拒绝", () => command("拒绝申请", r.public_id)); button(actions, "屏蔽此人", () => command("屏蔽", r.sender_code), "quiet");
      } else button(actions, "撤销申请", () => command("撤销申请", r.public_id));
    }
  }
}
function renderMe(page) {
  const p = state.profile; node("h1", p?.nickname || "我的资料", page);
  if (p) {
    node("p", "成员编号 " + p.member_code, page, "muted"); const profile = disclosure(page, "昵称与个人介绍");
    form(profile, "昵称", value => command("编辑昵称", value), {value: p.nickname, max: 40, button: "保存昵称"});
    form(profile, "简短介绍", value => command("编辑介绍", value), {value: p.bio, required: false, multiline: true, button: "保存介绍"});
    const contact = disclosure(page, "联系方式");
    node("p", "粘贴自己的好友链接或输入账号，保存一次即可。每次交换前会展示本次分享的账号。", contact, "muted");
    const contactInput = form(contact, "好友链接或 Delta Chat 账号", value => {
      if (value.includes("://") || value.toLowerCase().includes("i.delta.chat/#")) {
        const account = accountFromInput(value);
        if (!account) { note("请检查好友链接格式。"); return; }
        send("command", {command: "设置联系方式", value: account, verify_current_account: true});
      } else command("设置联系方式", value);
    }, {value: p.share_contact || "", max: 2000, multiline: true, placeholder: "粘贴好友链接或输入 name@example.org", button: "保存联系方式"});
    const contactHint = node("p", "", contact, "muted contact-hint");
    const updateHint = () => { const account = accountFromInput(contactInput.value); contactHint.textContent = account ? "识别到账号：" + account : contactInput.value.trim() ? "请检查好友链接或账号格式。" : ""; };
    contactInput.addEventListener("input", updateHint); updateHint();
    if (p.communication_address) {
      node("p", "当前私聊账号：" + p.communication_address, contact, "muted");
      button(contact, "使用当前账号", () => send("use_current_account"));
    }
    const settings = node("section", null, page, "card"); node("h2", "隐私设置", settings);
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
  const networks = disclosure(page, "我的网络");
  for (const n of state.networks) button(networks, (n.current ? "当前 · " : "切换 · ") + n.name, () => command("切换网络", n.public_id));
  button(networks, "加入新网络", () => send("join"), "primary");
  const safety = disclosure(page, "屏蔽、举报与账号");
  form(safety, "屏蔽成员编号", value => command("屏蔽", value), {button: "屏蔽", max: 30});
  form(safety, "解除屏蔽成员编号", value => command("解除屏蔽", value), {button: "解除屏蔽", max: 30});
  button(safety, "查看屏蔽列表", () => command("屏蔽列表"));
  form(safety, "举报：成员编号 + 空格 + 原因", value => command("举报", value), {button: "提交举报", max: 500, multiline: true});
  button(safety, "恢复原账号", () => command("恢复账号")); button(safety, "退出当前网络", () => command("退出网络"), "danger"); button(safety, "删除我的数据", () => command("删除我的数据"), "danger");
  const info = disclosure(page, "隐私说明与使用帮助"); privacy(info);
  node("p", "仅公开资料的成员会出现在目录中。申请 7 天后过期；双方确认后交换联系方式，请自行核验身份。", info, "muted");
  node("p", "点击按钮即可操作，结果自动同步。不需要返回聊天发送指令，也不需要网页后台账号。首次获取或更新时才需发送“小程序”。", info, "muted");
  button(page, "管理网络 ›", () => { adminView = true; adminConfirm = null; render(); send("admin_open"); }, "admin-entry");
}
function adminOperation(operation, data = {}, question = "") {
  if (question) {
    adminConfirm = {operation, data, question};
    document.activeElement?.blur(); render(); revealOperation("#admin-confirm"); return;
  }
  send("admin_action", {operation, data});
}
function revealOperation(selector, focus = true) {
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const region = document.querySelector(selector) || document.querySelector("#admin-section");
    if (!region) return;
    document.querySelectorAll(".operation-focus").forEach(el => el.classList.remove("operation-focus"));
    region.classList.add("operation-focus");
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
  button(top, "‹ 返回", () => { adminView = false; adminConfirm = null; render(); }, "quiet");
  if (state.admin?.unlocked) button(top, "撤销本账号授权", () => { adminConfirm = null; send("admin_lock"); }, "quiet");
  node("h1", "网络管理", page);
  const data = state.admin;
  if (!data) { node("div", "正在打开管理工具…", page, "empty muted"); return; }
  if (!data.unlocked) {
    const card = node("section", null, page, "card admin-unlock");
    node("h2", "验证管理员密钥", card);
    node("p", "验证后为当前账号保留管理权限。", card, "muted");
    if (state.receipt && !state.receipt.ok && state.messages.length) node("p", state.messages[state.messages.length - 1], card, "admin-error");
    if (!data.challenge) {
      node("p", "请获取验证信息后输入密钥。", card, "muted");
      button(card, "获取验证信息", () => send("admin_open"), "primary");
    } else form(card, "管理员密钥", unlockAdmin, {type: "password", autocomplete: "off", clearOnSubmit: true, max: 128, button: "解锁管理"});
    return;
  }
  if (state.messages.length) node("p", state.messages[state.messages.length - 1], page, "admin-result");
  if (adminConfirm) {
    const confirm = node("section", null, page, "card confirmation"); confirm.id = "admin-confirm";
    node("h2", "确认操作", confirm); node("p", adminConfirm.question, confirm);
    button(confirm, "确认", () => { if (!adminConfirm) return; const c = adminConfirm; adminConfirm = null; send("admin_action", {operation: c.operation, data: {...c.data, confirmed: "yes"}}); }, "danger");
    button(confirm, "取消", () => { const code = adminConfirm?.data.member_code; adminConfirm = null; render(); revealOperation(code ? '[data-member-code="' + CSS.escape(code) + '"]' : "#admin-section"); }, "quiet");
  }
  const networks = data.networks || [], current = networks.find(n => n.id === data.current);
  if (networks.length) {
    const chooser = node("label", "管理网络", page, "admin-network-label");
    const select = node("select", null, chooser, "admin-select");
    for (const n of networks) { const option = node("option", n.name, select); option.value = String(n.id); option.selected = n.id === data.current; }
    select.onchange = () => { adminConfirm = null; send("admin_select", {admin_network: Number(select.value), page: 0}); };
  }
  const sections = data.system ? ["网络", "成员", "审核", "记录", "系统"] : ["网络", "成员", "审核", "记录"];
  if (!sections.includes(adminSection)) adminSection = "网络";
  const tabs = node("div", null, page, "admin-tabs");
  for (const section of sections) button(tabs, section, () => { adminSection = section; adminConfirm = null; render(); revealOperation("#admin-section", false); }, section === adminSection ? "selected" : "quiet");
  page = node("section", null, page); page.id = "admin-section";
  if (!current && adminSection !== "系统") { node("div", "暂无可管理的网络。", page, "empty muted"); return; }
  if (adminSection === "网络") {
    const card = node("section", null, page, "card");
    node("h2", current.name, card); node("p", current.public_id + " · " + current.member_count + " 位成员", card, "muted");
    const row = node("div", null, card, "setting"); node("span", "允许新成员加入", row);
    const toggle = button(row, "", () => adminOperation(current.join_enabled ? "join_off" : "join_on"), "ios-switch");
    toggle.setAttribute("role", "switch"); toggle.setAttribute("aria-label", "允许新成员加入"); toggle.setAttribute("aria-checked", String(Boolean(current.join_enabled))); node("span", null, toggle, "switch-knob");
    adminButtonGroup(card, [["查看加入码", () => adminOperation("view_code")], ["更换加入码", () => adminOperation("rotate", {}, "更换后，旧加入码将失效。现有成员不受影响。")]]);
    if (data.system) {
      form(card, "修改网络名称", value => adminOperation("rename", {name: value}), {value: current.name, max: 60, button: "保存名称"});
      button(card, current.status === "active" ? "停用网络" : "恢复网络", () => adminOperation(current.status === "active" ? "disable" : "enable", {}, current.status === "active" ? "停用后，该网络暂时无法使用。" : "确定恢复该网络？"), current.status === "active" ? "danger" : "secondary");
    }
  } else if (adminSection === "成员") {
    const members = data.members || [];
    if (!members.length) node("div", "没有成员。", page, "empty muted");
    for (const m of members) {
      const detail = disclosure(page, m.nickname + " · " + m.member_code + " · " + (m.is_self ? "本人" : m.discoverable ? "首页可见" : "资料隐藏"));
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
      if (m.status === "active" && m.role === "member") form(detail, "封禁原因", value => adminOperation("ban", {member_code: m.member_code, reason: value}, "确定封禁 " + m.nickname + "？"), {max: 300, button: "封禁成员"});
      if (m.status === "active" && m.role === "network_admin" && data.system) node("p", "移除或封禁前，请先撤销管理员权限。", detail, "muted");
    }
    const paging = node("div", null, page, "pagination");
    if (data.page > 0) button(paging, "上一页", () => send("admin_select", {admin_network: current.id, page: data.page - 1}));
    if (data.page > 0 || data.has_more) node("span", "第 " + (data.page + 1) + " 页", paging, "muted");
    if (data.has_more) button(paging, "下一页", () => send("admin_select", {admin_network: current.id, page: data.page + 1}));
    if (data.bans?.length) {
      const banned = disclosure(page, "封禁名单");
      for (const m of data.bans) {
        const row = node("div", null, banned, "setting"); node("span", m.nickname + " · " + m.member_code, row);
        button(row, "解除封禁", () => adminOperation("unban", {member_code: m.member_code}, "解除 " + m.nickname + " 的封禁？"));
      }
    }
  } else if (adminSection === "审核") {
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
      const card = node("section", null, page, "card"); node("h3", r.nickname + " · " + r.member_code, card); node("p", r.public_id, card, "muted");
      adminButtonGroup(card, [["批准恢复", () => adminOperation("approve_recovery", {recovery_id: r.public_id}, "确认已核实 " + r.nickname + " 的身份？"), "primary"], ["拒绝", () => adminOperation("reject_recovery", {recovery_id: r.public_id}, "确定拒绝该恢复申请？"), "danger"]]);
    }
  } else if (adminSection === "记录") {
    const names = {"web:view_join_code":"查看加入码", "create_network":"创建网络", "rename_network":"修改网络名称", "set_role":"更改管理员", "remove_member":"移除成员", "ban_member":"封禁成员", "unban_member":"解除封禁", "rotate_join_code":"更换加入码", "enable_network":"恢复网络", "disable_network":"停用网络", "approve_account_recovery":"批准账号恢复", "reject_account_recovery":"拒绝账号恢复"};
    if (!(data.audit || []).length) node("div", "暂无管理记录。", page, "empty muted");
    for (const a of data.audit || []) { const card = node("div", null, page, "card admin-audit"); node("strong", names[a.action] || "管理操作", card); if (a.target_ref) node("p", a.target_ref, card); node("small", (a.created_at || "").replace("T", " ").slice(0, 16), card, "muted"); }
  } else if (adminSection === "系统") {
    const card = node("section", null, page, "card"); node("h2", "创建网络", card);
    form(card, "网络名称", value => adminOperation("create", {name: value}), {max: 60, button: "创建网络"});
    node("p", "系统会生成加入码，请妥善保管。", card, "muted");
  }
}
function render() {
  const current = state.networks.find(n => n.current); $("network").textContent = current?.name || state.pending?.network_name || "好友助手";
  const page = $("page"), wizard = $("wizard");
  if (wizard.parentElement === page) page.parentElement.insertBefore(wizard, page);
  renderWizard(); page.replaceChildren();
  const setup = bound && ready && ["guide_join_code", "guide_nickname", "join"].includes(state.pending?.action);
  page.hidden = setup;
  if (setup) {
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
    form(card, "网络加入码", value => send("join", {value}), {max: 200, placeholder: "输入管理员提供的加入码", button: "下一步"});
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
    if (state.pending?.action === "send_request") page.querySelector("h1")?.after(wizard);
  } else renderMe(page);
  renderNav();
}
function renderNav() {
  $("nav").replaceChildren();
  const icons = {"好友": "M4 20v-1a6 6 0 0 1 12 0v1M10 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8ZM18 8h5M20.5 5.5v5", "申请": "M4 5h16v14H4zM7 9h10M7 13h7", "消息": "M4 6a3 3 0 0 1 3-3h10a3 3 0 0 1 3 3v8a3 3 0 0 1-3 3H10l-5 4v-4a3 3 0 0 1-1-3z", "我的": "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8ZM4 21a8 8 0 0 1 16 0"};
  for (const name of ["好友", "申请", "消息", "我的"]) {
    const b = button($("nav"), "", () => {
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
$("refresh").onclick = () => waiting ? resend() : send("refresh");
$("retry").onclick = resend;
let viewportFrame;
let keyboardScroll = null;
let restingHeight = window.innerHeight;
let viewportWidth = window.innerWidth;
const editableFocused = () => document.activeElement?.matches("input,textarea,select,[contenteditable=true]");
function keepFocusedControlVisible() {
  const active = document.activeElement, main = document.querySelector("main");
  if (!active || !main.contains(active) || !active.matches("input,textarea,select,[contenteditable=true]")) return;
  const box = main.getBoundingClientRect(), form = active.closest("form");
  const target = form && form.getBoundingClientRect().height < main.clientHeight - 24 ? form : active;
  const rect = target.getBoundingClientRect();
  if (rect.bottom > box.bottom - 12) main.scrollTop += rect.bottom - box.bottom + 12;
  else if (rect.top < box.top + 12) main.scrollTop -= box.top + 12 - rect.top;
}
function fitViewport() {
  cancelAnimationFrame(viewportFrame);
  viewportFrame = requestAnimationFrame(() => {
    const viewport = window.visualViewport;
    // Do not reflow the app in response to accessibility pinch zoom.
    if (viewport && Math.abs(viewport.scale - 1) > .01) return;
    const main = document.querySelector("main");
    if (Math.abs(window.innerWidth - viewportWidth) > 80) {
      restingHeight = window.innerHeight; viewportWidth = window.innerWidth;
    }
    const height = viewport?.height || window.innerHeight;
    const keyboardOpen = (editableFocused() || keyboardScroll !== null) && height < restingHeight - 100;
    if (!keyboardOpen) {
      if (!editableFocused() && keyboardScroll === null) restingHeight = Math.max(height, window.innerHeight);
      document.documentElement.style.setProperty("--app-height", height + "px");
      document.documentElement.style.setProperty("--app-top", "0px");
      window.scrollTo(0, 0);
      if (keyboardScroll !== null) {
        main.scrollTop = keyboardScroll; keyboardScroll = null;
        if (editableFocused()) document.activeElement.blur();
      }
    } else {
      if (keyboardScroll === null) keyboardScroll = main.scrollTop;
      document.documentElement.style.setProperty("--app-height", height + "px");
      document.documentElement.style.setProperty("--app-top", Math.max(0, viewport?.offsetTop || 0) + "px");
      requestAnimationFrame(keepFocusedControlVisible);
    }
  });
}
window.visualViewport?.addEventListener("resize", fitViewport);
window.visualViewport?.addEventListener("scroll", fitViewport);
window.addEventListener("resize", fitViewport);
document.addEventListener("focusin", fitViewport);
document.addEventListener("focusout", () => { fitViewport(); [100, 300, 600, 1000].forEach(ms => setTimeout(fitViewport, ms)); });
fitViewport();
render(); if (waiting) watchPending();
if (window.webxdc?.setUpdateListener && bound) {
  Promise.resolve(window.webxdc.setUpdateListener(update => {
    const p = update.payload;
    if (p?.type === "contact-notification" && typeof p.id === "string" && typeof p.body === "string") {
      if (!notifications.some(n => n.id === p.id)) notifications = [...notifications, {id: p.id, body: p.body, at: p.at || ""}].slice(-200);
      if (tab === "消息") { readNotifications[p.id] = true; try { localStorage.setItem("friend-notifications-read", JSON.stringify(readNotifications)); } catch {} }
      if (tab === "消息") render(); else renderNav();
      return;
    }
    if (!p || p.type !== "contact-state" || !Array.isArray(p.networks) || !Array.isArray(p.messages)) return;
    const before = JSON.stringify({...state, at: null, receipt: null});
    const previousStep = state.pending?.action;
    const changed = p.current_network !== state.current_network || !p.profile;
    const deletedProfile = Boolean(state.profile) && !p.profile;
    const unlockSucceeded = waiting && waiting.kind === "admin_unlock" && p.receipt?.request_id === waiting.id && p.receipt.ok;
    state = {...p, requests: p.requests || [], choices: p.choices || [], messages: unlockSucceeded ? [] : p.messages.length || changed ? p.messages : state.messages};
    ready = true; if (changed) { selected = null; autoNetwork = null; if (deletedProfile) notifications = []; }
    const receivedReceipt = Boolean(waiting && p.receipt?.request_id === waiting.id);
    const adminReceipt = receivedReceipt && ["admin_open", "admin_unlock", "admin_action", "admin_select", "admin_lock"].includes(waiting.kind) ? waiting.kind : null;
    if (receivedReceipt) {
      const showConfirmation = waiting.openMessages && p.receipt.ok && p.pending?.action === "send_request";
      waiting = null; rememberPending(); clearTimeout(waitTimer); $("retry").hidden = true;
      if (showConfirmation) {
        tab = "消息"; selected = null;
        notifications.forEach(n => { readNotifications[n.id] = true; });
        try { localStorage.setItem("friend-notifications-read", JSON.stringify(readNotifications)); } catch {}
        scrollTop();
      }
      note(p.receipt.ok ? "" : (p.messages[p.messages.length - 1] || "操作未完成，请重试。"));
    } else if (!waiting) note("");
    if (before !== JSON.stringify({...state, at: null, receipt: null})) renderFromRemote(receivedReceipt);
    if (adminReceipt && adminView) {
      if (!p.receipt.ok) revealOperation(".admin-error, #status");
      else if (!state.admin?.unlocked) revealOperation(".admin-unlock", false);
      else revealOperation(adminReceipt === "admin_action" ? ".admin-result" : "#admin-section", false);
    }
    if (receivedReceipt && previousStep !== state.pending?.action && ["guide_join_code", "guide_nickname", "join"].includes(previousStep)) scrollTop();
    if (!waiting && !p.notifications_enabled && !registrationAttempted) {
      registrationAttempted = true; setTimeout(() => send("refresh"), 0);
    } else if (!waiting && p.notifications_enabled && p.profile && !p.directory && autoNetwork !== p.current_network) {
      autoNetwork = p.current_network; setTimeout(() => send("directory", {page: 0}), 0);
    }
  }, 0)).catch(() => note("状态读取失败。请重新打开，或向机器人发送“小程序”。"));
} else note(window.webxdc ? "请先向好友助手获取应用。" : "浏览器仅可预览界面；请在 Delta Chat 中打开 .xdc 文件。");
