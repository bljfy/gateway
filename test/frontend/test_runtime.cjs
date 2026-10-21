"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
class Element {
  constructor() {
    this.children = []; this.dataset = {}; this.textContent = ""; this.hidden = false;
    this.classList = {toggle() {}};
  }
  append(...items) {this.children.push(...items);}
  replaceChildren() {this.children = [];}
}
const elements = new Map();
const document = {
  title:"",
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  },
  createElement() {return new Element();},
  querySelectorAll() {return [];}
};
const context = vm.createContext({
  document, GatewaySessions:{setRole() {}}, ManualUI:{controls() {}, render() {}},
  renderConnections() {}, renderSimulation() {}
});
const source = fs.readFileSync(path.resolve(__dirname, "../../src/gateway/webui/static/app.js"), "utf8");
vm.runInContext(source.slice(0, source.indexOf("const connectionLabels")), context);
const state = {
  role:"client", config_loaded:true, request_phase:"idle", authenticated:false,
  auth_test:{phase:"idle"}, auth_test_totals:{success:0, failed:0}
};
const render = () => context.render(state);
const el = id => document.getElementById(id);
render();
assert.equal(el("metrics").children.length, 6, "client metrics must never be empty");
assert.match(el("auth-test-title").textContent, /尚未测试/);
el("runtime-metrics").open = false;
el("runtime-events").open = false;
state.auth_test = {phase:"running", target_id:"gateway", target_host:"127.0.0.1", target_port:18443};
state.busy = true;
render();
assert.equal(el("phase").textContent, "认证测试中");
assert.equal(el("test").disabled, true);
assert.match(el("auth-test-meta").textContent, /127.0.0.1:18443/);
assert.equal(el("runtime-metrics").open, false, "polling must preserve collapsed details");
assert.equal(el("runtime-events").open, false);
state.busy = false;
state.auth_test = {...state.auth_test, phase:"success", duration_ms:125, finished_at:"2026-10-21T12:00:00Z"};
render();
assert.match(el("auth-test-title").textContent, /认证成功/);
assert.match(el("auth-test-message").textContent, /模型服务需通过发送推理请求验证/);
assert.match(el("auth-test-meta").textContent, /125 ms/);
assert.equal(el("test").disabled, false);
state.auth_test.phase = "failed";
render();
assert.equal(el("auth-test-result").dataset.phase, "failed");
assert.match(el("auth-test-message").textContent, /信任公钥/);
state.auth_test = {phase:"idle"};
render();
assert.equal(el("auth-test-meta").textContent, "");
assert.equal(el("auth-test-meta").hidden, true);
state.role = "adapter";
render();
assert.equal(el("metrics").children[0].children[1].textContent, "启动服务后显示");
console.log("Runtime display checks passed");
