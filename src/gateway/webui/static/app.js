"use strict";
const $ = id => document.getElementById(id);
const titles = {client: "客户端 · 推理操作台", gateway: "网关 · 安全控制台", adapter: "Adapter · 推理服务台"};
const errors = {setup_failed:"身份初始化失败。请检查 GmSSL 原生库清单和目录权限；修复后加载配置，或使用新的身份目录。", setup_exists:"身份目录已存在，请加载已有配置。", config_invalid:"配置校验失败。请检查本地文件、角色、身份、原生库及连接参数。", config_required:"请先加载本地身份配置。", busy:"当前有任务或服务正在运行，请先停止。", service_failed:"服务启动或运行失败。请检查端口占用、凭据、审计目录和本地配置。", connection_failed:"网关认证失败。请检查目标地址、信任公钥、有效期和对端服务。", inference_failed:"推理未完成。请检查网关与 Adapter 状态、模型凭据或超时设置。", request_invalid:"请求参数无效或超过限额。", socket_required:"页面连接已断开，请等待重新连接后再发送。", origin_rejected:"访问地址校验失败，请使用部署时配置的完整地址。", request_mismatch:"请求已变化，请刷新状态后重试。", capacity_rejected:"页面连接数量已达上限。", request_too_large:"请求体过大，请缩短提示词或检索片段。"};
let state = {}, settings = {}, peers = [], owner = null, currentId = null;
let dirty = false, actionBusy = false, socket = null, closing = false, submitting = false;
let answer = null, answerFrame = null;
async function setupGuide(force = false) {
  const setup = await api("setup");
  $("setup-guide").hidden = setup.ready && !force;
  $("setup-summary").textContent = !setup.identity_ready
    ? (setup.can_initialize ? "首次运行，请创建配套身份配置。" : "身份配置未能加载。请在下方检查服务器上的配置路径；已有身份目录需要修复，不能覆盖生成。")
    : (setup.ready ? "配置已就绪。可按以下步骤检查服务；更新 API Key 前请先停止 Adapter 服务。" : "身份已准备好，请填写模型 API Key。服务地址可在下方连接配置中修改。");
  $("setup-initialize").hidden = !setup.can_initialize;
  $("setup-key-fields").hidden = !setup.identity_ready || state.role !== "adapter";
  $("setup-key-name").replaceChildren(...setup.credential_names.map(name => {
    const option = document.createElement("option"); option.value = name;
    option.textContent = name + (setup.missing_credentials.includes(name) ? " · 待填写" : " · 已配置"); return option;
  }));
}
$("setup-refresh").onclick = () => action(() => setupGuide(true));
$("setup-show").onclick = () => action(() => setupGuide(true));
$("setup-initialize").onclick = () => action(async () => {
  await api("setup", "POST", {initialize:true});
  render(await api("state")); fill(await api("config")); await setupGuide();
});
$("setup-save-key").onclick = () => action(async () => {
  const key = $("setup-key").value; $("setup-key").value = "";
  await api("setup", "POST", {api_key:key, api_key_env:$("setup-key-name").value, persist:$("setup-persist").checked});
  await setupGuide(); render(await api("state"));
  notice("API Key 已保存。请启动 Adapter，再到客户端测试认证并发送问题。");
});

function renderAnswer(immediate = false) {
  if (immediate && answerFrame !== null) { cancelAnimationFrame(answerFrame); answerFrame = null; }
  const paint = () => {
    answerFrame = null;
    $("output").innerHTML = AnswerPresentation.render(answer?.text || "");
    $("result-placeholder").hidden = Boolean(answer?.text);
    $("output").scrollTop = $("output").scrollHeight;
  };
  if (immediate) paint();
  else if (answerFrame === null) answerFrame = requestAnimationFrame(paint);
}

function notice(message) { $("notice").textContent = message || ""; $("notice").hidden = !message; }
function message(code) { return errors[code] || "操作失败，请检查本地配置和服务状态。"; }
async function api(path, method = "GET", data) {
  const headers = {"X-WebUI-Request":"1"};
  if (data !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch("/api/" + path, {method, headers, body:data === undefined ? undefined : JSON.stringify(data), cache:"no-store"});
  const result = await response.json();
  if (!response.ok) throw new Error(message(result.error));
  return result;
}
async function action(callback) {
  if (actionBusy) return;
  actionBusy = true; notice(""); controls();
  try { await callback(); }
  catch (error) { notice(error.message || "连接失败，请确认本地 WebUI 仍在运行。"); }
  finally { actionBusy = false; controls(); }
}
function configured() {
  if (dirty) throw new Error("连接参数尚未保存。请先保存连接设置，再执行操作。");
  if (!state.config_loaded) throw new Error(errors.config_required);
}
function controls() {
  const running = ["starting","running","stopping"].includes(state.phase);
  const locked = running || state.busy || actionBusy;
  document.querySelectorAll(".config-card input,.config-card select,.config-card textarea").forEach(el => {el.disabled = !!locked;});
  $("load").disabled = !!locked;
  $("setup-initialize").disabled = !!locked;
  $("setup-save-key").disabled = !!locked;
  $("save").disabled = !!locked || !state.config_loaded;
  $("test").disabled = !!locked || !state.config_loaded;
  $("start").disabled = !!locked || !state.config_loaded;
  $("stop-service").disabled = actionBusy || !running;
  $("infer").disabled = !!locked || !owner || !state.config_loaded || submitting;
  $("cancel").disabled = actionBusy || !state.busy || !currentId;
  $("clear").disabled = !!state.busy || submitting;
  $("simulation-start").disabled = !!locked || !owner || !state.config_loaded;
  $("simulation-stop").disabled = actionBusy || state.simulation?.phase !== "running";
  for (const id of ["simulation-mode", "simulation-count", "simulation-duration"]) $(id).disabled = !!locked;
  $("dirty-note").textContent = dirty ? "有未保存的连接参数，请先保存。" : "设置保存到独立旁文件，身份与信任文件保持原样。";
}
function metric(label, value) {
  const box = document.createElement("div"); box.className = "metric";
  const caption = document.createElement("span"); caption.textContent = label;
  const number = document.createElement("strong"); number.textContent = String(value);
  box.append(caption, number); $("metrics").append(box);
}
function render(next) {
  state = next;
  $("title").textContent = titles[state.role] || "本地控制台";
  document.title = $("title").textContent + " · 国密推理";
  document.querySelectorAll(".role-item").forEach(el => el.classList.toggle("active", el.dataset.role === state.role));
  document.querySelectorAll(".client-only").forEach(el => {el.hidden = state.role !== "client";});
  document.querySelectorAll(".server-only").forEach(el => {el.hidden = state.role === "client";});
  document.querySelectorAll(".adapter-only").forEach(el => {el.hidden = state.role !== "adapter";});
  document.querySelectorAll(".gateway-only").forEach(el => {el.hidden = state.role !== "gateway";});
  document.querySelectorAll(".outbound-only").forEach(el => {el.hidden = state.role === "adapter";});
  document.querySelectorAll(".gateway-only").forEach(el => {el.hidden = state.role !== "gateway";});
  GatewaySessions.setRole(state.role);
  if (state.role !== "gateway") {$("runtime-events").open = true; $("runtime-metrics").open = true;}
  $("config-status").textContent = state.config_loaded ? "身份已加载" : "尚未加载";
  const phase = {stopped:"未启动",starting:"启动中",running:"运行中",stopping:"停止中",failed:"服务失败"};
  $("phase").textContent = state.role === "client" ? (state.busy ? "操作进行中" : "等待操作") : phase[state.phase];
  $("phase").classList.toggle("error", state.phase === "failed");
  $("runtime-note").textContent = state.role === "client" ? (state.authenticated ? "最近一次网关双向认证成功；每次请求独立建连。" : "连接测试执行 SM2 双向认证，不仅检查端口连通。") : state.role === "adapter" ? (state.model_verified ? "本次运行已有模型请求完成。" : "服务监听就绪不代表模型服务已验证。") : "展示本角色进程的会话、审计与转发状态。";
  $("metrics").replaceChildren();
  const m = state.metrics || {};
  if (state.role !== "client") {
    const labels = {active_sessions:"活跃安全会话", pending_sessions:"待认证会话", requests_inflight:"推理进行中", requests_completed_total:"请求完成", requests_failed_total:"请求失败", requests_cancelled_total:"请求取消", handshake_success_total:"入站认证成功", handshake_failure_total:"入站认证失败", authenticated_sessions_total:"网关认证成功", audit_healthy:"审计健康", integrity_failure_total:"完整性拒绝", replay_rejected_total:"重放拒绝"};
    for (const [key,label] of Object.entries(labels)) if (key in m && !(state.role === "gateway" && key === "authenticated_sessions_total")) metric(label, key === "audit_healthy" ? (m[key] ? "正常" : "异常") : m[key]);
  }
  if (state.error) notice(message(state.error));
  if (currentId && state.request_id === currentId && state.request_phase !== "idle") resultStatus(state.request_phase);
  renderConnections(state.connections);
  renderSimulation(state.simulation);
  controls();
}
const connectionLabels = {handshaking:"握手中",active:"已认证",closed:"已关闭",authentication_failed:"认证失败",protocol_rejected:"协议拒绝",timeout:"超时",capacity:"容量拒绝",stopped:"服务停止",failed:"连接失败"};
function cells(row, values) {
  for (const value of values) { const cell = document.createElement("td"); cell.textContent = String(value); row.append(cell); }
}
function connectionRows(target, items, recent) {
  const fragment = document.createDocumentFragment();
  for (const item of items) {
    const row = document.createElement("tr");
    const phase = recent ? item.result : item.phase;
    cells(row, [item.id.slice(0,8) + " · " + item.source, item.peer || "尚未认证", connectionLabels[phase] || phase, item.duration_seconds + " 秒", item.inflight, item.received_bytes + " / " + item.sent_bytes + " B"]);
    if (recent && !["closed","stopped"].includes(phase)) row.className = "connection-error";
    fragment.append(row);
  }
  $(target).replaceChildren(fragment);
}
function renderConnections(data) {
  if (!data) return;
  $("connection-count").textContent = data.total_active + " 条进行中";
  $("connection-empty").hidden = data.total_active > 0;
  $("connection-truncated").hidden = !data.truncated;
  connectionRows("connections", data.active, false);
  connectionRows("recent-connections", data.recent, true);
}
function renderSimulation(data) {
  if (!data) return;
  $("simulation-phase").textContent = {running:"模拟进行中",completed:"批次已结束",cancelled:"已停止"}[data.phase] || data.phase;
  const labels = {connecting:"连接中",authenticated:"已认证并保持",heartbeating:"正在发送心跳",tcp_connected:"TCP 已连接",closed:"已结束",held:"保持时间结束",heartbeat_completed:"心跳保持结束",gracefully_closed:"已完成关闭协商",tcp_disconnected:"握手前已主动断开",peer_closed:"网关已关闭连接",hold_elapsed:"到达保持时间",disconnected:"已主动中断",unexpected_data:"收到非预期数据",unexpected_record:"收到非预期记录",connection_failed:"连接失败或被关闭",cancelled:"已停止"};
  const fragment = document.createDocumentFragment();
  for (const client of data.clients) { const row = document.createElement("tr"); cells(row, ["客户端 " + client.client, labels[client.phase] || client.phase, labels[client.result] || "—"]); fragment.append(row); }
  $("simulation-clients").replaceChildren(fragment);
  const active = data.clients.filter(client => client.phase !== "closed").length;
  $("simulation-summary").textContent = data.clients.length + " 个客户端 · " + active + " 个进行中";
}
function fill(data) {
  settings = data.settings; peers = data.peers || [];
  $("target_id").replaceChildren();
  peers.forEach(peer => {const option = document.createElement("option"); option.value = peer.peer_id; option.textContent = peer.peer_id + (peer.enabled ? "" : "（已停用）"); option.disabled = !peer.enabled; $("target_id").append(option);});
  for (const [key,value] of Object.entries(settings)) if ($(key)) $(key).value = value;
  dirty = false; controls();
}
function configuration() {
  const result = {};
  for (const key of Object.keys(settings)) result[key] = key.endsWith("_port") ? Number($(key).value) : $(key).value;
  return result;
}
function event(item) {
  $("no-events")?.remove();
  const row = document.createElement("li"), time = document.createElement("time"), value = document.createElement("span");
  time.textContent = new Date(item.time).toLocaleTimeString("zh-CN", {hour12:false});
  time.dateTime = item.time; value.textContent = item.message;
  row.append(time, value); $("events").prepend(row);
  while ($("events").children.length > 128) $("events").lastElementChild.remove();
}
function resultStatus(phase) {
  const labels = {running:"生成中",completed:"已完成",cancelled:"已停止 · 未完成",failed:"失败 · 未完成"};
  $("result-status").textContent = labels[phase] || "等待请求";
  $("result-status").classList.toggle("error", ["failed","cancelled"].includes(phase));
}
function connect() {
  socket = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/api/ws");
  socket.onmessage = e => {
    const item = JSON.parse(e.data);
    if (item.type === "ready") {
      owner = item.owner; $("socket-status").textContent = "页面已连接"; $("socket-status").classList.remove("error");
      $("events").replaceChildren(); item.events.forEach(event); render(item.state);
    } else if (item.type === "state") render(item.state);
    else if (item.type === "event") event(item);
    else if (item.type === "output" || item.type === "finished") {
      if (submitting && currentId === null) currentId = item.request_id;
      if (item.request_id !== currentId) return;
      if (answer === null) answer = new AnswerPresentation.AnswerBuffer(item.request_id);
      if (item.type === "output") {answer.append(item.output); renderAnswer();}
      else {answer.finish(item.result === "completed"); renderAnswer(true); resultStatus(item.result); if (item.error) notice(message(item.error));}
    }
  };
  socket.onclose = () => {
    owner = null; $("socket-status").textContent = "页面连接已断开"; $("socket-status").classList.add("error");
    if (currentId && state.busy) {answer?.finish(false); renderAnswer(true); resultStatus("failed");}
    controls(); if (!closing) setTimeout(connect, 2000);
  };
}
$("load").onclick = () => action(async () => {fill(await api("config/load", "POST", {config_path:$("config_path").value})); render(await api("state")); await setupGuide(); notice("配置已加载。可修改连接参数并保存。");});
$("save").onclick = () => action(async () => {fill(await api("config", "PUT", configuration())); await setupGuide(); notice("连接设置已保存。");});
$("start").onclick = () => action(async () => {configured(); render(await api("start", "POST"));});
$("stop-service").onclick = () => action(async () => {render(await api("stop", "POST"));});
$("test").onclick = () => action(async () => {configured(); render(await api("test", "POST"));});
$("infer").onclick = () => action(async () => {
  configured(); if (!$("prompt").value.trim()) throw new Error("请输入提示词。");
  currentId = null; answer = null; submitting = true; renderAnswer(true);
  resultStatus("running");
  try {
    const data = await api("infer", "POST", {owner, prompt:$("prompt").value, model:$("model").value, max_output_tokens:Number($("max_output_tokens").value), retrieval_context:[...document.querySelectorAll("#contexts textarea")].map(el=>el.value), stream:$("stream").checked});
    currentId = data.request_id;
    render(await api("state"));
  } catch (error) {resultStatus("failed"); throw error;}
  finally {submitting = false;}
});
$("cancel").onclick = () => action(async () => {render(await api("cancel", "POST", {request_id:currentId}));});
$("simulation-start").onclick = () => action(async () => {
  configured();
  const count = Number($("simulation-count").value), duration = Number($("simulation-duration").value);
  if (!Number.isInteger(count) || count < 1 || count > 32 || !Number.isInteger(duration) || duration < 1 || duration > 60) throw new Error("客户端数须为 1–32，保持时间须为 1–60 秒的整数。");
  currentId = null;
  render(await api("simulation", "POST", {owner, count, duration, mode:$("simulation-mode").value}));
});
$("simulation-stop").onclick = () => action(async () => {render(await api("simulation/stop", "POST"));});
$("add-context").onclick = () => {
  if ($("contexts").children.length >= 64) return;
  const row = document.createElement("div"), area = document.createElement("textarea"), remove = document.createElement("button");
  row.className = "context-item"; area.rows = 2; area.placeholder = "检索片段内容"; area.setAttribute("aria-label", "检索片段");
  remove.textContent = "移除"; remove.className = "text-button"; remove.onclick = () => row.remove(); row.append(area,remove); $("contexts").append(row);
};
$("copy").onclick = () => action(async () => {await navigator.clipboard.writeText(answer?.text || ""); notice("结果已复制。");});
$("clear").onclick = () => {currentId = null; answer = null; renderAnswer(true); resultStatus("idle");};
document.querySelectorAll(".config-card input,.config-card select,.config-card textarea").forEach(el => el.addEventListener("input", () => {dirty = true; controls();}));
$("target_id").addEventListener("change", () => {const peer = peers.find(p=>p.peer_id === $("target_id").value); if (peer && peer.host) {$("target_host").value = peer.host; $("target_port").value = peer.port;} dirty = true; controls();});
window.addEventListener("beforeunload", () => {closing = true; socket?.close(); GatewaySessions.close();});
const simulationDescriptions = {
  normal:"完成 SM2 认证后保持连接，到达时限后正常关闭；网关应显示已认证连接。",
  stalled_handshake:"只建立 TCP，不发送握手数据；保持时间超过网关握手超时后可观察超时关闭。",
  malformed_frame:"发送单字节非法协议内容；对照网关最近结束记录中的协议拒绝。",
  abrupt_disconnect:"认证后直接中断 TCP，省略关闭协商；对照网关的异常结束记录。",
  heartbeat:"认证后每秒发送一次合法空心跳，持续到时限；网关应保持连接且不产生模型请求。",
  graceful_close:"认证成功后立即执行 CLOSE / CLOSE_ACK 协商；网关应记录正常关闭。",
  tcp_disconnect:"TCP 建立后立即关闭，不完成认证；网关应记录未认证连接结束。",
  empty_frame:"只发送长度为零的帧头；网关应拒绝非法帧长度。",
  oversized_frame:"只声明超过协议上限的长度，不发送大载荷；网关应立即拒绝。",
  truncated_frame:"声明 16 字节帧体但只发送 1 字节，然后关闭发送方向；网关应拒绝截断帧。",
  unexpected_record:"认证后发送 RESPONSE 记录，验证网关拒绝客户端使用错误业务类型。",
  out_of_order_fragment:"认证后发送首片序号为 1 的未结束请求，验证分片顺序检查；不调用模型。"
};
function describeSimulation() { $("simulation-description").textContent = simulationDescriptions[$("simulation-mode").value] || ""; }
$("simulation-mode").addEventListener("change", describeSimulation);
describeSimulation();
action(async () => {render(await api("state")); fill(await api("config")); await setupGuide(); connect();});
