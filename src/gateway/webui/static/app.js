"use strict";
const $ = id => document.getElementById(id);
const titles = {client: "客户端 · 推理操作台", gateway: "网关 · 安全控制台", adapter: "Adapter · 推理服务台"};
const errors = {config_invalid:"配置校验失败。请检查本地文件、角色、身份、原生库及连接参数。", config_required:"请先加载本地身份配置。", busy:"当前有任务或服务正在运行，请先停止。", service_failed:"服务启动或运行失败。请检查端口占用、凭据、审计目录和本地配置。", connection_failed:"网关认证失败。请检查目标地址、信任公钥、有效期和对端服务。", inference_failed:"推理未完成。请检查网关与 Adapter 状态、模型凭据或超时设置。", request_invalid:"请求参数无效或超过限额。", socket_required:"页面连接已断开，请等待重新连接后再发送。", origin_rejected:"本地来源校验失败，请使用启动时显示的 127.0.0.1 地址。", request_mismatch:"请求已变化，请刷新状态后重试。", capacity_rejected:"页面连接数量已达上限。", request_too_large:"请求体过大，请缩短提示词或检索片段。"};
let state = {}, settings = {}, peers = [], owner = null, currentId = null;
let dirty = false, actionBusy = false, socket = null, closing = false, submitting = false;
let answer = null, answerFrame = null;

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
  document.querySelectorAll(".config-card input,.config-card select").forEach(el => {el.disabled = !!locked;});
  $("load").disabled = !!locked;
  $("save").disabled = !!locked || !state.config_loaded;
  $("test").disabled = !!locked || !state.config_loaded;
  $("start").disabled = !!locked || !state.config_loaded;
  $("stop-service").disabled = actionBusy || !running;
  $("infer").disabled = !!locked || !owner || !state.config_loaded || submitting;
  $("cancel").disabled = actionBusy || !state.busy || !currentId;
  $("clear").disabled = !!state.busy || submitting;
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
  controls();
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
  socket = new WebSocket("ws://" + location.host + "/api/ws");
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
$("load").onclick = () => action(async () => {fill(await api("config/load", "POST", {config_path:$("config_path").value})); render(await api("state")); notice("配置已加载。可修改连接参数并保存。");});
$("save").onclick = () => action(async () => {fill(await api("config", "PUT", configuration())); notice("连接设置已保存。");});
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
$("add-context").onclick = () => {
  if ($("contexts").children.length >= 64) return;
  const row = document.createElement("div"), area = document.createElement("textarea"), remove = document.createElement("button");
  row.className = "context-item"; area.rows = 2; area.placeholder = "检索片段内容"; area.setAttribute("aria-label", "检索片段");
  remove.textContent = "移除"; remove.className = "text-button"; remove.onclick = () => row.remove(); row.append(area,remove); $("contexts").append(row);
};
$("copy").onclick = () => action(async () => {await navigator.clipboard.writeText(answer?.text || ""); notice("结果已复制。");});
$("clear").onclick = () => {currentId = null; answer = null; renderAnswer(true); resultStatus("idle");};
document.querySelectorAll(".config-card input,.config-card select").forEach(el => el.addEventListener("input", () => {dirty = true; controls();}));
$("target_id").addEventListener("change", () => {const peer = peers.find(p=>p.peer_id === $("target_id").value); if (peer && peer.host) {$("target_host").value = peer.host; $("target_port").value = peer.port;} dirty = true; controls();});
window.addEventListener("beforeunload", () => {closing = true; socket?.close(); GatewaySessions.close();});
action(async () => {render(await api("state")); fill(await api("config")); connect();});
