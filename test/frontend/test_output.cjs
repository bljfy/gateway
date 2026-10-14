"use strict";

// 仅使用 Node 内置模块，生产 WebUI 无需安装 Node 或 npm 包。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.resolve(__dirname, "../../src/gateway/webui/static");
const context = vm.createContext({atob: globalThis.atob});
for (const file of ["markdown-it.min.js", "output.js"]) {
  vm.runInContext(fs.readFileSync(path.join(root, file), "utf8"), context, {filename: file});
}
const {AnswerBuffer, render} = context.AnswerPresentation;
const id = "12345678-1234-4234-8234-123456789abc";
const header = JSON.stringify({timestamp:"2026-10-14T10:00:00.123+00:00",request_id:id});
const markdown = "# 回答\n\n**中文 🙂** 与 `inline`。\n\n- 甲\n- 乙\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n```python\nprint('<tag>')\n```\n";
let count = 0;
function test(name, callback) {
  callback(); count += 1; console.log("PASS " + name);
}
test("普通响应只保留正文", () => {
  const answer = new AnswerBuffer(id); answer.append(header + "\n" + markdown); answer.finish(true);
  assert.equal(answer.text, markdown);
});
test("逐字符流式元数据从未进入正文", () => {
  const answer = new AnswerBuffer(id);
  for (const character of header + "\n") {answer.append(character); assert.equal(answer.text, "");}
  for (const character of markdown) answer.append(character);
  answer.finish(true); assert.equal(answer.text, markdown);
});
test("所有分片边界均正确恢复", () => {
  const payload = header + "\r\n" + markdown;
  for (let i = 0; i <= payload.length; i++) {
    const answer = new AnswerBuffer(id); answer.append(payload.slice(0, i)); answer.append(payload.slice(i));
    answer.finish(true); assert.equal(answer.text, markdown);
  }
});
for (const body of ["普通文本无换行", "# 无元数据\n\n正文", '{"value":1}', '{"value":1}\n正文', "{" + "x".repeat(600), header.replace(id, "other-id") + "\n正文", '{"timestamp":"unknown","request_id":"' + id + '"}\n正文', "null\n正文"]) {
  test("保留正文 " + body.slice(0, 20), () => {
    const answer = new AnswerBuffer(id); answer.append(body); answer.finish(true); assert.equal(answer.text, body);
  });
}
test("取消或断流不展示未收齐的元数据", () => {
  for (let i = 0; i < header.length; i++) {
    const answer = new AnswerBuffer(id); answer.append(header.slice(0, i)); answer.finish(false); assert.equal(answer.text, "");
  }
});
test("元数据无正文与末尾无换行", () => {
  for (const data of [header, header + "\n", ""]) {
    const answer = new AnswerBuffer(id); answer.append(data); answer.finish(true); assert.equal(answer.text, "");
  }
});
test("取消保留已收到的模型正文", () => {
  const answer = new AnswerBuffer(id); answer.append(header + "\n## 部分\n\n**正文"); answer.finish(false);
  assert.equal(answer.text, "## 部分\n\n**正文"); assert.match(render(answer.text), /<h2>部分<\/h2>/);
});
test("渲染标题列表表格与安全代码块", () => {
  const html = render(markdown);
  for (const fragment of ["<h1>回答</h1>", "<strong>中文 🙂</strong>", "<ul>", "<table>", '<code class="language-python">', "&lt;tag&gt;"]) assert.ok(html.includes(fragment));
});
test("正文 HTML 只作为文字呈现", () => {
  const html = render('<script>alert(1)</script>\n<img src=x onerror=alert(1)>\n<svg onload=alert(1)>');
  assert.doesNotMatch(html, /<(?:script|img|svg)\b/i); assert.ok(html.includes("&lt;script&gt;"));
});
for (const url of ["javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html;base64,PHNjcmlwdD4=", "vbscript:alert(1)", "file:///C:/private", "/api/state", "//attacker.example", "java&#x73;cript:alert(1)"]) {
  test("拒绝危险或非允许链接 " + url, () => assert.doesNotMatch(render(`[链接](${url})`), /<a\b/i));
}
test("允许 HTTPS 链接但隔离新页面", () => {
  const html = render("[文档](https://example.com)");
  assert.match(html, /href="https:\/\/example.com"/); assert.match(html, /target="_blank"/);
  assert.match(html, /rel="noopener noreferrer"/);
});
test("图片只展示替代文字，不能自动外联", () => {
  const html = render("![安全说明](https://attacker.example/pixel)");
  assert.doesNotMatch(html, /<img\b|\bsrc=/i); assert.match(html, /安全说明/);
});
test("未结束的代码围栏安全呈现", () => {
  const html = render('```html\n<script>alert(1)</script>');
  assert.match(html, /<pre><code/); assert.doesNotMatch(html, /<script>/);
});
console.log(`${count} presentation scenarios passed (including all metadata split boundaries)`);
