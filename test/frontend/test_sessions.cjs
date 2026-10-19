"use strict";

// Minimal DOM stand-in verifies polling does not destroy a reader's selection.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
class Element {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.dataset = {}; this.className = "";
    this.open = false; this.replacements = 0; this.scrollTop = 0;
    this.scrollHeight = 100; this.clientHeight = 100; this.listeners = {};
    this.classList = {toggle: (name, on) => {
      const names = new Set(this.className.split(" ").filter(Boolean));
      if (on) names.add(name); else names.delete(name); this.className = [...names].join(" ");
    }};
  }
  append(...nodes) {this.children.push(...nodes);}
  replaceChildren(...nodes) {this.replacements++; this.children = nodes;}
  querySelectorAll(selector) {
    return this.children.flatMap(child => [
      ...(selector === "details[open]" && child.tagName === "details" && child.open ? [child] : []),
      ...child.querySelectorAll(selector)
    ]);
  }
  setAttribute() {}
  addEventListener(name, callback) {this.listeners[name] = callback;}
  showModal() {this.open = true;}
  close() {this.open = false; this.listeners.close?.();}
}
const elements = new Map();
const document = {
  getElementById(id) {if (!elements.has(id)) elements.set(id, new Element("div")); return elements.get(id);},
  createElement(tag) {return new Element(tag);}
};
const id = "12345678-1234-4234-8234-123456789abc";
const chat = {id:"connection:" + id, request_id:id, connection_id:"connection", peer:"已认证客户端", model:"m", stream:true, phase:"ok", started_at:"2026-10-19T10:00:00Z", prompt:"测试问题", output:"# 完整回答", truncated:false};
let summary = {connections:[], conversations:[{...chat, prompt:undefined, output:undefined}]};
let tick;
const context = vm.createContext({
  document, atob:globalThis.atob,
  setInterval(callback) {tick = callback; return 1;}, clearInterval() {},
  async fetch(url) {return {ok:true, async json() {return structuredClone(url === "/api/sessions" ? summary : chat);}};}
});
const root = path.resolve(__dirname, "../../src/gateway/webui/static");
for (const file of ["markdown-it.min.js", "output.js", "sessions.js"]) {
  vm.runInContext(fs.readFileSync(path.join(root, file), "utf8"), context, {filename:file});
}
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  context.GatewaySessions.setRole("gateway"); await flush();
  document.getElementById("past-chats").onclick(); await flush();
  const list = document.getElementById("session-list");
  assert.match(list.children[0].textContent, /已认证客户端/);
  list.children[0].onclick(); await flush();
  const pane = document.getElementById("conversation");
  const count = pane.replacements;
  assert.match(pane.children[1].children[1].innerHTML, /<h1>完整回答<\/h1>/);
  for (let index = 0; index < 3; index++) {await tick(); await flush();}
  assert.equal(pane.replacements, count, "unchanged history must retain DOM selection and focus");
  chat.output += "\n\n新增正文";
  await tick(); await flush();
  assert.equal(pane.replacements, count + 1, "changed response must repaint");
  document.getElementById("session-dialog").close();
  chat.output += "\n关闭后更新";
  await tick(); await flush();
  assert.equal(pane.replacements, count + 1, "closed dialog must not load or repaint its body");
  document.getElementById("past-chats").onclick(); await flush();
  list.children[0].onclick(); await flush();
  assert.match(pane.children[1].children[1].innerHTML, /关闭后更新/);
  assert.match(list.children[0].textContent, /已认证客户端/, "evicted connection must retain chat peer");
  context.GatewaySessions.close();
  console.log("PASS unchanged history DOM, changed content, dialog reopen and retained peer");
})().catch(error => {console.error(error); process.exitCode = 1;});
