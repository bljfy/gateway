"use strict";
window.ManualUI = (() => {
  const q = id => document.getElementById(id);
  let helpers;
  const labels = {connecting:"连接中",active:"安全连接已建立",raw_connected:"TCP 已连接",closing:"正在清理",closed:"实验已结束",peer_closed:"观察到对端关闭",authentication_failed:"客户端认证校验失败",timeout:"实验时限或会话期限到达",connection_failed:"连接失败或对端中断",cancelled:"已结束实验",connected:"连接建立",finished:"实验结束并清理",key_modified:"已应用替代 SM4 密钥",key_restored:"已恢复协商密钥",send_sent:"记录已发送",tamper_sent:"篡改记录已发送",reflect_sent:"反射记录已发送",replay_sent:"本连接捕获帧已重放",cross_session_sent:"上次连接捕获帧已重放",raw_sent:"原始帧已发送",transport_aborted:"TCP 已主动中断",handshake_mutated:"握手消息已篡改",record_received:"收到已认证记录",raw_data_received:"收到 TCP 数据"};
  const secretIds = ["manual-signing-private","manual-signing-public","manual-encryption-public","manual-sm4"];
  const secureActions = ["manual-key","manual-restore","manual-send","manual-tamper","manual-reflect"];
  function controls(state, actionBusy, owner) {
    const phase = state.manual?.phase, active = phase === "active";
    const connected = active || phase === "raw_connected";
    const locked = actionBusy || state.busy;
    q("manual-open").disabled = !!locked || !owner || !state.config_loaded;
    q("manual-close").disabled = actionBusy || !state.busy || !["connecting","active","raw_connected"].includes(phase);
    for (const id of secureActions) q(id).disabled = actionBusy || !owner || !active;
    for (const id of ["manual-abort","manual-raw-send"]) q(id).disabled = actionBusy || !owner || !connected;
    q("manual-replay").disabled = actionBusy || !owner || !connected || !state.manual?.captured_bytes;
    q("manual-cross").disabled = actionBusy || !owner || !connected || !state.manual?.previous_bytes;
    q("manual-reflect").disabled = actionBusy || !owner || !active || !state.manual?.received_bytes;
    document.querySelectorAll(".manual-connect-fields input,.manual-connect-fields select").forEach(el => {el.disabled = !!locked;});
  }
  function render(data) {
    if (!data) return;
    q("manual-phase").textContent = labels[data.phase] || data.phase;
    q("manual-connection").textContent = (data.source ? "客户端源地址 " + data.source + " · " : "") + "下一发送序号 " + (data.next_sequence ?? "—");
    q("manual-key-state").textContent = data.key_modified ? "本次发送使用替代 SM4 密钥。" : "使用握手协商的密钥。";
    q("manual-capture").textContent = "当前捕获 " + data.captured_bytes + " B · 上次连接捕获 " + data.previous_bytes + " B · 剩余操作 " + data.actions_remaining;
    q("manual-result").textContent = data.result ? (labels[data.result] || data.result) + "。具体拒绝原因请对照网关记录。" : "等待手动操作；观察记录不代表网关验收结论。";
    const fragment = document.createDocumentFragment();
    for (const step of data.steps) { const li = document.createElement("li"); li.textContent = new Date(step.time).toLocaleTimeString("zh-CN",{hour12:false}) + "  " + (labels[step.action] || step.action) + (step.bytes ? " · " + step.bytes + " B" : ""); fragment.append(li); }
    q("manual-steps").replaceChildren(fragment);
  }
  async function command(data) {
    helpers.render(await helpers.api("manual/action", "POST", {owner:helpers.owner(), command:data}));
  }
  function record(action) {
    const data = {action, sequence:q("manual-sequence").value, session_id:q("manual-session").value, direction:Number(q("manual-direction").value), record_type:Number(q("manual-record-type").value), request_id:q("manual-request-id").value, chunk_index:Number(q("manual-chunk").value), payload:q("manual-payload").value, nonce:q("manual-nonce").value};
    if (action === "tamper") { if (q("manual-offset").value !== "") data.offset = Number(q("manual-offset").value); data.xor = Number(q("manual-xor").value); }
    return data;
  }
  function initialize(context) {
    helpers = context;
    q("manual-open").onclick = () => helpers.action(async () => {
      helpers.configured(); helpers.resetRequest();
      const options = {mode:q("manual-mode").value, duration:Number(q("manual-duration").value), fault:q("manual-fault").value, identity:q("manual-identity").value, key_version:Number(q("manual-version").value), expired_trust:q("manual-expired").checked, signing_private:q("manual-signing-private").value, gateway_signing_public:q("manual-signing-public").value, gateway_encryption_public:q("manual-encryption-public").value};
      try { helpers.render(await helpers.api("manual/open", "POST", {owner:helpers.owner(), options})); }
      finally { for (const id of secretIds) q(id).value = ""; }
    });
    q("manual-close").onclick = () => helpers.action(() => command({action:"close"}));
    q("manual-key").onclick = () => helpers.action(async () => { try { await command({action:"key",key:q("manual-sm4").value}); } finally {q("manual-sm4").value = "";} });
    q("manual-restore").onclick = () => helpers.action(() => command({action:"restore_key"}));
    for (const action of ["send","tamper","reflect"]) q("manual-" + action).onclick = () => helpers.action(() => command(record(action)));
    for (const [id,action] of [["manual-replay","replay"],["manual-cross","cross_session"],["manual-abort","abort"]]) q(id).onclick = () => helpers.action(() => command({action}));
    q("manual-raw-send").onclick = () => helpers.action(() => command({action:"raw",raw:q("manual-raw").value}));
  }
  return {initialize, controls, render};
})();
