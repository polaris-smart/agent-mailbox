# agent-mailbox v0.8 维护入口

当前公开 Beta 为 `0.8.0b4`，GitHub/PyPI 发行物源于 `420d136`，来源与完整验证见 [Beta 4 记录](evidence/v080/beta4-release.md)。后续文档提交不改写该 tag 或已上传文件；程序变更使用新版本并重新通过门禁。只有 GitHub 与 PyPI 两个发行渠道，没有既有 TestPyPI、Homebrew 或 npm 入口。

先读 [PRD](PRD.md)、[独立实现决定](designs/2026-09-30-v080-independent.md) 和 [审查记录](reviews/2026-09-30-v080-independence.md)。员工卡片及信箱见 [设计](designs/2026-09-30-employee-cards-mailboxes.md) 和 [验证](evidence/v080/employee-cards-mailboxes.md)。当前接入与 NoFox / Apache-2.0 署名见 [设计](designs/2026-09-30-connection-guidance.md) 和 [验证](evidence/v080/connection-guidance.md)。本轮员工与项目组见 [设计](designs/2026-09-30-project-groups.md) 和 [验证](evidence/v080/project-groups.md)。v0.8 完全独立，不继续承载 v0.7 运行路径；旧 Git 历史和 docs/archive/v07 只供参考。

默认命令 `agent-mailbox` / `python -m agent_mailbox` 进入工作台；`prepare` 显式准备本 home 的锁定运行组件；`node` 管理可选无界面执行节点。产品状态放在 home/workbench；不要修改任何在用 v0.7 目录、全局 agent 配置或原生登录。

使用 graph 发现代码。当前 AOCI 索引原有布局错误 `code_object_path_unresolved`，rules/overview 无法建立可靠完整认知；如仍如此，记录真实工具错误并从绑定源码验证，不能伪造 index 或 receipt。CodeGraph 是可用的源码导航手段。稳定后按项目 AGENTS 调用 AOCI maintain。

开发校验：安装 `.[dev]`，Ruff check/format，pytest。完整 bridge 校验需固定 Node 和 runtime deps，通过 `AGENT_MAILBOX_TEST_RUNTIME_DIR` 显式提供；缺依赖的 skip 不是执行成功。CI 已配置受管 Node/npm 依赖，本机结果不能代称 GitHub CI 已绿。

各入口共用私有 SQLite、同 home owner lock、相同项目授权规则。不要增加第二个调度器。远端结果有持久 outbox 和同 run 幂等确认；不确定执行中断后不能自动重复。新变更应保留审批默认拒绝、取消优先、人工验收、项目权限隔离和凭据脱敏。

产品和数据目录分开；备份必须在停止应用后进行或用一致性数据库备份。构建 app 不等于 Developer ID 签名、公证；本机 ad-hoc seal 不等于公开发行。代码提交、bundle 是代码备份，不能作为用户运行数据已备份的证明。

### 0.8.0a7 员工信箱

员工 `project_messages(folder="inbox"|"sent"|"group", limit=1..100)` 默认收件；本人广播在 sent。本地/远端工具和受管启动上下文使用绑定身份，不读取他人定向消息作为最近消息。管理员与设备级授权上下文仍为共享范围，不能声称端到端保密；任务/产物本身为项目共享。查看无 ACK/任务副作用。路线图建议见 docs/designs/2026-09-30-v08-user-journey-roadmap.md，未经确认不得把其余提案写成 PRD 事实。
