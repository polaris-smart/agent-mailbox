# v0.8.0a4：员工名册与项目组验证

2026-09-30，本机 macOS arm64 / Python 3.13。对应设计：[项目组](../../designs/2026-09-30-project-groups.md)。

## 已交付

员工先独立登记，随后加入项目；区分 app、CLI、服务入口，不把发现、原生登录、受管执行成功合并为一个绿色状态。支持按钮和拖拽入组、普通消息与回复线程、显式协作请求、请求去重和有限请求链。普通通信不创建执行任务。仅 Codex / Claude CLI 有受管执行适配器；其余入口可登记并加入项目，不能宣称已自动触发。

项目工具写入绑定当前 employee/project/task/run 与短期执行凭据；旧成员凭据仅用于授权读取。员工消息标记受管会话来源，内部实际作者未验证：不能声称已阻止继承父凭据的 subagent 冒名。迁移从 schema 5 到 6，迁移前生成私有一致性备份，并保留员工、成员关系、任务及历史。

同时修复权限请求超时后的控制消息会话归属：取消和权限控制携带 session_id；明确来自其他会话的控制仍拒绝，晚到的同会话错误不再掩盖真实终态。

## 验证与边界

- 完整 pytest：253 项，252 通过，1 跳过，123.265 秒；固定运行组件通过 AGENT_MAILBOX_TEST_RUNTIME_DIR 提供。跳过项要求仅 acpx 的缺适配器环境，本次完整环境不满足该条件。
- Ruff 检查和格式、JS 语法及 git diff 空白检查通过。
- 真实隔离 HTTP / SQLite / worker 浏览器验证共 10 项，含零项目名册、app/CLI 登记、入组、普通信不触发、回复线程、协作请求、人工验收、390px 页面、连接复查和拖拽。发现、认证和模型进程使用明确测试替身，因此这些结果不代表真实模型协作。
- 本机发现 18 个安装入口：9 个 CLI、9 个 app；重复安装入口分别展示。这是入口清单，不是 18 名可自动执行员工。
- 实际 Codex 受管执行：显式选用服务返回的 gpt-6-luna，成功读取项目上下文、发送协作请求及普通回执，任务到达人工验收 review。CLI 默认 gpt-6.1-sol 曾返回 MODEL_UNSUPPORTED，未静默降级。
- 实际 Claude：协作接收任务及指定原生 executable 的独立复测均返回 AUTH_REQUIRED；原生登录检查成功不等于受管执行认证成功。没有修改全局登录、复制凭据或把失败算作回执。两个真实员工完整往返尚未通过。
- 非 editable 安装 wheel 与冻结 app 均验证 0.8.0a4：空 home 启动、发现并登记真实 Hermes app、创建项目加入、普通信零任务、不支持的执行请求拒绝、owner 退出；没有模型调用。

本次不代表 Windows/Linux app 发现、跨设备实际联调、GitHub CI、公开发布或 Developer ID 签名/公证已完成。产物仅有本机 ad-hoc 签名。

AOCI rules、context_compaction overview 仍返回已有 index_invalid / code_object_path_unresolved；完整认知索引不可用。使用绑定源码与 CodeGraph 导航，不伪造索引或认知回执，收尾调用 maintain 并保留实际诊断。

## 后续

优先排清 Claude 受管认证，再增加有真实测试依据的执行适配器。产品线负责人、考核入组、派单时指定 provider/model/key 保留为后续范围；本版本没有新增凭据分发平台。
