"use strict";

// 响应首行可能跨多个流式分片；决定其是否为元数据前不展示该前缀。
(() => {
  const HEADER_LIMIT = 512;
  class AnswerBuffer {
    constructor(requestId) {
      this.requestId = requestId;
      this.prefix = "";
      this.text = "";
      this.readingHeader = true;
    }
    isMetadata(line) {
      try {
        const value = JSON.parse(line);
        return value !== null && typeof value === "object" && !Array.isArray(value)
          && Object.keys(value).length === 2 && value.request_id === this.requestId
          && typeof value.timestamp === "string"
          && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value.timestamp)
          && Number.isFinite(Date.parse(value.timestamp));
      } catch { return false; }
    }
    append(chunk) {
      if (!chunk) return;
      if (!this.readingHeader) { this.text += chunk; return; }
      this.prefix += chunk;
      const newline = this.prefix.indexOf("\n");
      if (newline !== -1) {
        this.text += this.isMetadata(this.prefix.slice(0, newline))
          ? this.prefix.slice(newline + 1) : this.prefix;
      } else if (this.prefix.startsWith("{") && this.prefix.length <= HEADER_LIMIT) {
        return;
      } else {
        this.text += this.prefix;
      }
      this.prefix = "";
      this.readingHeader = false;
    }
    finish(completed) {
      if (this.readingHeader) {
        // 正常完成时保留无换行的普通 JSON；取消时不泄漏未收齐的元数据首行。
        if (completed && !this.isMetadata(this.prefix)) this.text += this.prefix;
        this.prefix = "";
        this.readingHeader = false;
      }
    }
  }

  const markdown = globalThis.markdownit({html: false, linkify: false, typographer: false});
  const validate = markdown.validateLink.bind(markdown);
  markdown.validateLink = url => validate(url) && /^(?:https?:|mailto:|#)/i.test(url);
  // 模型输出不自动请求图片地址，避免外部资源跟踪；图片以替代文字呈现。
  markdown.renderer.rules.image = (tokens, index) => markdown.utils.escapeHtml(tokens[index].content);
  const openLink = markdown.renderer.rules.link_open
    || ((tokens, index, options, env, renderer) => renderer.renderToken(tokens, index, options));
  markdown.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
    tokens[index].attrSet("target", "_blank");
    tokens[index].attrSet("rel", "noopener noreferrer");
    return openLink(tokens, index, options, env, renderer);
  };
  globalThis.AnswerPresentation = {AnswerBuffer, render: text => markdown.render(text)};
})();
