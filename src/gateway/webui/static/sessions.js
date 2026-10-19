"use strict";

// Conversation bodies are fetched only while their local dialog is selected.
(() => {
  const $ = id => document.getElementById(id);
  const labels = {pending:"认证中", active:"已连接", closed:"已断开", running:"生成中", ok:"已完成", failed:"失败 · 未完成", cancelled:"已取消 · 未完成", timeout:"超时 · 未完成"};
  let enabled = false, timer = null, data = {connections:[], conversations:[]};
  let mode = "online", selected = null, version = 0, busy = false;
  let summaryKey = "";
  let renderedChatKey = "";
  const time = value => value ? new Date(value).toLocaleString("zh-CN", {hour12:false}) : "—";
  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  async function get(path) {
    const response = await fetch("/api/sessions" + path, {headers:{"X-WebUI-Request":"1"}, cache:"no-store"});
    if (!response.ok) throw new Error(response.status === 404 ? "记录已过期，请选择其他对话。" : "会话加载失败，请稍后重试。");
    return response.json();
  }
  function error(id, message = "") {$(id).textContent = message; $(id).hidden = !message;}
  function peer(id) {return data.connections.find(item => item.id === id)?.peer || "待认证客户端";}
  function connectionRow(item) {
    const row = element("details", undefined, "connection-row"); row.dataset.id = item.id;
    const summary = element("summary");
    summary.append(element("span", item.peer || "待认证客户端"), element("span", labels[item.phase], "badge"), element("time", time(item.disconnected_at || item.connected_at), "muted"));
    const details = element("div", undefined, "connection-details");
    for (const [label, value] of [["连接标识", item.id], ["认证结果", item.peer ? "双向认证成功" : "尚未完成认证"], ["来源地址", item.address || "未知"], ["连接时间", time(item.connected_at)], ["断开时间", time(item.disconnected_at)]]) {
      details.append(element("p", `${label}：${value}`));
    }
    const chats = data.conversations.filter(chat => chat.connection_id === item.id);
    if (!chats.length) details.append(element("p", "暂无对话，可能仅进行了认证测试。", "muted"));
    for (const chat of chats) {
      details.append(element("p", `请求：${chat.request_id} · ${time(chat.started_at)} · ${labels[chat.phase]}`));
      const button = element("button", `${chat.model} · ${labels[chat.phase]} · 查看对话`, "secondary");
      button.onclick = () => {open(chat.phase === "running" ? "online" : "past"); select(chat.id);};
      details.append(button);
    }
    row.append(summary, details); return row;
  }
  function renderConnections() {
    const opened = new Set([...$("connections").querySelectorAll("details[open]")].map(node => node.dataset.id));
    const active = data.connections.filter(item => item.phase !== "closed");
    $("connection-count").textContent = `${active.length} 条在线 · ${data.connections.filter(item => item.phase === "closed").length} 条已断开`;
    $("connections").replaceChildren(...active.map(item => {const row = connectionRow(item); row.open = opened.has(item.id); return row;}));
    if (!active.length) $("connections").append(element("p", "暂无在线连接。已断开的连接可通过上方按钮查看。", "muted"));
    $("online-chats").textContent = `在线对话 (${data.conversations.filter(item => item.phase === "running").length})`;
    $("past-chats").textContent = `历史对话 (${data.conversations.filter(item => item.phase !== "running").length})`;
    $("closed-connections").textContent = `已断开连接 (${data.connections.filter(item => item.phase === "closed").length})`;
  }
  function renderList() {
    const list = $("session-list");
    const opened = new Set([...list.querySelectorAll("details[open]")].map(node => node.dataset.id));
    list.replaceChildren();
    if (mode === "closed") {
      for (const item of data.connections.filter(item => item.phase === "closed")) {
        const row = connectionRow(item); row.open = opened.has(item.id); list.append(row);
      }
    } else {
      for (const item of data.conversations.filter(item => (item.phase === "running") === (mode === "online"))) {
        const button = element("button", `${item.peer || peer(item.connection_id)} · ${item.model}\n${labels[item.phase]} · ${time(item.started_at)}`, "conversation-choice");
        button.classList.toggle("selected", item.id === selected);
        button.setAttribute("aria-pressed", String(item.id === selected));
        button.onclick = () => select(item.id); list.append(button);
      }
    }
    if (!list.children.length) list.append(element("p", "暂无记录。", "muted"));
  }
  async function loadChat() {
    if (!selected || !$("session-dialog").open) return;
    const identity = selected, revision = version;
    try {
      const chat = await get("/" + encodeURIComponent(identity));
      if (revision !== version || identity !== selected || !$("session-dialog").open) return;
      const key = JSON.stringify(chat);
      error("dialog-error");
      if (key === renderedChatKey) return;
      const pane = $("conversation"), bottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 50;
      const scroll = pane.scrollTop;
      const prompt = element("div", undefined, "chat-bubble user-message");
      prompt.append(element("strong", "用户"), element("p", chat.prompt));
      const answer = new AnswerPresentation.AnswerBuffer(chat.request_id); answer.append(chat.output);
      if (chat.phase !== "running") answer.finish(chat.phase === "ok" && !chat.truncated);
      const reply = element("div", undefined, "chat-bubble assistant-message");
      reply.append(element("strong", `模型 · ${labels[chat.phase]}`));
      const body = element("div", undefined, "markdown-body");
      body.innerHTML = AnswerPresentation.render(answer.text);
      if (!answer.text) body.textContent = chat.phase === "running" ? "等待模型输出…" : "未收到模型正文。";
      reply.append(body); pane.replaceChildren(prompt, reply);
      if (chat.truncated) pane.append(element("p", "内容已达到展示上限，仅保留前 65536 字符；完整结果请在客户端查看。", "muted"));
      pane.scrollTop = bottom ? pane.scrollHeight : scroll; error("dialog-error");
      renderedChatKey = key;
    } catch (failure) {if (revision === version) error("dialog-error", failure.message);}
  }
  function select(identity) {
    selected = identity; version += 1; renderedChatKey = ""; $("conversation").replaceChildren(element("p", "加载中…", "muted")); renderList(); loadChat();
  }
  function open(nextMode) {
    mode = nextMode; selected = null; version += 1; renderedChatKey = "";
    $("session-dialog-title").textContent = {online:"在线对话", past:"历史对话", closed:"已断开连接"}[mode];
    $("conversation").replaceChildren(element("p", "选择一条对话查看内容。", "muted")); error("dialog-error");
    if (!$("session-dialog").open) $("session-dialog").showModal(); renderList(); refresh();
  }
  async function refresh() {
    if (!enabled || busy) return;
    busy = true;
    try {
      const next = await get(""), key = JSON.stringify(next);
      data = next;
      if (key !== summaryKey) {summaryKey = key; renderConnections(); if ($("session-dialog").open) renderList();}
      error("session-error");
      if ($("session-dialog").open) await loadChat();
    } catch (failure) {error("session-error", failure.message);}
    finally {busy = false;}
  }
  $("online-chats").onclick = () => open("online");
  $("past-chats").onclick = () => open("past");
  $("closed-connections").onclick = () => open("closed");
  $("close-session-dialog").onclick = () => $("session-dialog").close();
  $("session-dialog").addEventListener("close", () => {selected = null; version += 1;});
  globalThis.GatewaySessions = {
    setRole(role) {
      if (enabled === (role === "gateway")) return;
      enabled = role === "gateway";
      if (enabled) {refresh(); timer = setInterval(refresh, 1000);}
      else {clearInterval(timer); timer = null;}
    },
    close() {enabled = false; clearInterval(timer);}
  };
})();
