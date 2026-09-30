# v0.8.0a5 接入与署名验证

2026-09-30。本机 macOS arm64。设计见 [接入指引](../../designs/2026-09-30-connection-guidance.md)。

- 完整固定运行组件 pytest：258 passed、1 skipped，126.37 秒。跳过仍是需要缺适配器环境的负例，非跨平台通过。
- Claude 原生最小请求完成；原有后台 AUTH_REQUIRED 复现。确认 acpx 默认不包含 user settings，而本机认证和模型路由配置在 user settings.env。
- 修复只对白名单内的 Claude 认证/路由环境做进程内复用，已有显式环境优先，其他 agent 不读取该配置。不持久化凭据、不修改原生配置、不启用 user hooks / plugins / permission overrides。相应有效配置、显式优先、非 Claude、不可信字段、异常 JSON 用例均通过。
- 修复后真实 Claude 后台最小请求 completed，返回预期固定文本。真实项目通信回信确已持久化，但完整任务 TIMEOUT：送达不等于执行终态成功，不能声称已通过完整双员工任务闭环。
- 真实 HTTP/SQLite/worker 浏览器项目流程 8 项及连接复查/拖拽 2 项通过（模型是明确替身）。另验证 NoFox 页脚、Apache 完整文本、保留 MIT 声明、受支持/不受支持入口指引、原生已登录而后台认证失败指引及移动布局。原生已登录不会被提示反复登录。

用户已明确选择 v0.8 Apache-2.0 与 NoFox 署名。根 LICENSE、NOTICE、pyproject、README 和页脚一致，既有 MIT 声明保留；早期 MIT 发行不追溯更改。冻结构建保留 Node、JavaScript 原依赖文件，并收集构建环境中 Python 分发的许可证。

本轮不宣称所有 app 已可自动触发；全机发现与登记、具体入口及原项目成员关系以交付目录 registration-verification.json 为准。公开 CI、跨设备部署、Developer ID 签名和公证未验证或未执行。
