# 第三方浏览器资源

`src/gateway/webui/static/markdown-it.min.js` 为 markdown-it **15.0.2** 官方 UMD 浏览器构建，保持原始内容，不手工修改生成代码。

- 上游：https://github.com/markdown-it/markdown-it
- API 文档：https://markdown-it.github.io/markdown-it/
- 固定发布包：https://registry.npmjs.org/markdown-it/-/markdown-it-15.0.2.tgz
- 包内路径：`package/dist/browser/markdown-it.umd.min.js`
- 获取时校验 npm 发布元数据中的 SHA512 integrity。
- 浏览器文件 SHA256：`635972b985228e8af9f0143647c68616b7a3bb09f6946e7e4a52e43dcf5e7be5`
- 许可：MIT，随包保留 `markdown-it.LICENSE.txt`。
- 浏览器构建捆绑 mdurl、uc.micro、linkify-it、punycode.js（MIT）和 entities（BSD-2-Clause）；对应版权与许可通知保存在 `markdown-it.dependencies.LICENSE.txt`。argparse 仅用于上游命令行，不在浏览器构建中。

升级时先核查上游发布说明与浏览器 API，重新校验发布包与文件摘要，并运行前端安全、元数据分片测试和本地 WebUI 回归。页面仅加载本机静态资源，不请求 CDN。
