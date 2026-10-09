# v0.8 Beta 验收边界

2026-10-01。本文保留 Beta 1–3 的历史验收，并补充 Beta 4 发行门槛。目标版本 **0.8.0b4**，实际发行状态以对应 GitHub Release 和 PyPI 页面为准。默认路径：发现员工 → 登记并检查能力 → 创建项目组 → 加入员工和资料 → 分配任务 → 邮件交接 → human 验收。历史通过结果不等于后续提交的门禁已通过。

| 范围 | 当前能力（含 Beta 1 基线验证） | 边界 |
| --- | --- | --- |
| 员工 | 发现、登记、登录状态与执行能力分开；Codex、Claude CLI 真实交接 | 可发现 App 不代表可自动控制；不是所有 agent 均已验证 |
| 项目与邮件 | 项目成员、收件箱、显式任务、审批、取消、验收 | 普通邮件不暗中创建任务；没有通用拖拽 DAG workflow 编辑器 |
| 资料 | 实时查看与不可变版本分开；员工提案由 owner 确认；任务领取时固定批准版本 | 文本提案不覆盖源文件；没有后台 Git 同步；历史内容可能因新增凭据脱敏而改变返回校验值，会明确标记 |
| 资料展示 | PRD、todo、daily update、架构资料；离线单文件 HTML 隔离预览 | 阻止外部网络、父页面和工作台 API；依赖 CDN 的报告需转为离线单文件 |
| 工作日志 | 从持久任务事件、邮件、笔记和资料审批生成，只读 | 不等于防篡改审计；内部 subagent 作者身份没有独立认证，沿用父员工会话归属 |
| 检索工具 | 可选 CodeGraph 本机 CLI 与已有索引；失败原因与新鲜度未知可见 | 不自动建立索引（人侧 `agent-mailbox knowledge index --project <id> --yes` 可显式重建 ✓）；Windows 适配器明确不支持；**Graft 与 AOCI 的只读诊断已整合**（2026-10-07：5 个工具双面挂载 ✓ 含路径约束与陈旧门 ✓）；AOCI-Code 完整治理仍未整合 |
| 跨设备 | Mac 协调端 → Ubuntu ARM64 容器节点，配对、HTTPS 身份校验、授权、映射、任务回执、版本校验、断线恢复、撤销 | 节点任务使用确定性测试执行器；Ubuntu 原生 LLM 适配器和真实 HK/US 服务器未验证 |
| UI | 左侧团队/当前项目/管理分组，项目选择器，中英文，手机抽屉与键盘操作，带样式的协议与帮助 | 其他语言未声明支持 |
| 安装升级 | Mac ARM64 独立 wheel、打包 App、本机保留数据升级与迁移备份 | App 无 Developer ID 签名/公证；手动升级，无自动更新器；Beta 4 三平台包检查见下节，真实 Windows/Linux 模型仍未验 |

## Beta 4 发行检查

- 统一版本 `0.8.0b4`，GitHub tag 为 `v0.8.0b4`，原生包和 Python 发行物必须来自同一提交。只有 GitHub 与 PyPI；没有 TestPyPI、Homebrew 或 npm 发行。
- 提交 `3a2cfdb` 的 [CI 36846081917](https://github.com/polaris-smart/agent-mailbox/actions/runs/36846081917) 完整 9 作业通过：Mac/Linux × Python 3.10/3.13 每组 365 passed、4 skipped；Windows × Python 3.10/3.13 每组 321 passed、48 skipped。后续测试预算修正必须按对应提交重新执行门禁，最终结果见版本发行说明。
- 三平台原生包均实际压缩、解压、启动并验证认证、私有权限、版本、退出和 11 个 frozen MCP 工具；这些检查不调用模型。Windows CodeGraph 不支持，POSIX 专属 fixture 及信号/链接测试按平台跳过，不计为验证通过。
- 真实 Codex/Claude 的项目探针、独立编辑、固定交付审查及显式验收/应用在 Mac 验证；验收由获授权的 owner 测试脚本调用，不冒充人工点击。首次探针未调用工具的失败也保留。
- 本机 Beta 3 → Beta 4 升级完成私有一致性备份，员工、项目、任务及邮件表 hash 未变；真实浏览器 10 项检查通过、页面错误 0。后续提交的程序来源以构建记录为准。
- SQLite 更新备份显式关闭连接并转为独立 DELETE journal 文件，清单和文件使用实际平台私有权限；邀请导入同样检查 Windows 受保护 ACL。
- 原生包范围为 macOS ARM64、Windows x64、Linux x64，不提供 Intel Mac 或 Windows ARM 原生包。没有发行者签名或 macOS 公证。实体设备首次下载体验、真实 HK/US 部署及 Windows/Linux 原生模型调用没有被 CI 代替。

逐轮失败、修正和来源见 [Beta 4 证据](evidence/v080/beta4-release.md)。安装和保留数据升级见[安装指南](BETA-INSTALL.md)。

## Beta 1 基线验证记录（历史基线，非 Beta 3 重跑）

- Mac 完整回归：309 passed、1 skipped、0 failed（310 项）。
- Ubuntu ARM64 完整回归：269 passed、41 skipped、0 failed；41 项为未安装 Node/ACPX 的运行时协议测试，不冒充已验证。
- 真实 Codex → Claude CLI 完成项目资料读取、邮件交接、human 验收；源文件改变后仍读任务固定版本。
- 跨设备七项脚本通过；已结束、取消、撤销的执行会话无法继续访问项目工具。
- UI 29 项检查通过，包含真实 HTTP 数据流程和独立布局场景，无页面异常。
- 安装包和本机升级结果见[证据记录](evidence/v080/beta1.md)。

## 跨设备方式

使用单一协调端队列、节点身份和项目授权，不另建 SSH 派单队列。邀请不能替代网络可达性、agent 登录、路径映射与授权。局域网可直连；公网可使用 VPN 或 SSH 隧道。Mac 协调端睡眠或离线时，远端无法正常领取新任务。

Paperclip 的固定源码参考包含 SSH 执行目标及工作区传输，可覆盖 LAN 之外的服务器：[SSH 实现](https://github.com/paperclipai/paperclip/blob/0e5830887b0120e433712bdc61105c846b3d9fb4/packages/adapter-utils/src/ssh.ts)、[私有网络部署](https://github.com/paperclipai/paperclip/blob/0e5830887b0120e433712bdc61105c846b3d9fb4/docs/deploy/tailscale-private-access.md)。dsh-devices 的参考为[SSH 实现](https://github.com/polaris-smart/dsh-devices/blob/4e4216efb3faa7a5c57384d258f6331cd4438959/src/ssh/ssh.ts)。

完整 AI ERP、自动模型采购、成本推算、万能 App 控制、多人企业权限、独立向量服务留待后续。没有真实成本来源就显示未知。


## Beta 2 增量

新增更新设置/准备、私有备份、持久暂停、远端不确定领取保护与节点版本协议。安装升级仍手动完成，公开版本未发行。Beta 1 的真实 LLM/Ubuntu 证据为基础版本记录；Beta 2 增量验证详见[更新证据](evidence/v080/beta2-updates.md)，不得把历史 Ubuntu/LLM 结果冒充 Beta 2 的重跑结果。

## Beta 3 增量及验证状态

- 显式员工接入 probe：可选模型，实际上下文与随机标记笔记回执；查看检查不启动模型。取消/离组/退役不算成功。远端 probe 不支持，未适配 App 明确不可执行；原生账号仍共享。
- 本机 clean、committed Git 根目录的独立 worktree；非 Git/子目录/脏目录不降级原地修改。不是 OS 沙箱。远端 workspace-write 暂拒绝。
- 固定文本交付及校验值、来源分明的验证信息。Human 验收不等于合入；显式 apply 检查源仓库基线与干净状态，不 commit/push。退回创建关联任务并在基线一致时继承 patch。
- 项目工具新增 `project_delivery(task_id)`，共 11 项；读取本项目固定同事交付，不任意复制工作区文件。二进制、符号链接、凭据、不安全或过大 patch 不自动合入。
- SQLite/身份更新备份不含 home/task-workspaces、外部 Git 或供应商登录；工作区保留但须独立备份。

当前证据草稿：Mac 回归 349 passed、1 skipped；UI 27 项检查（原 20 项流程及 7 项模型 payload/元数据场景）、0 页面异常；7 项补充场景没有运行模型。Beta 3 真实 Codex/Claude 已通过 context/note probe、独立编辑、project_delivery 固定差异审查、Human 验收与显式合入。初次默认模型 MODEL_UNSUPPORTED 的失败另行保留，成功重跑由显式选择兼容模型 完成。Windows/Linux 实体设备和真实 HK/US 未验收。最终结果以[Beta 3 证据](evidence/v080/beta3-collaboration.md)及对应构建目录记录为准；历史 Beta 1/2 的 provider、Ubuntu 或安装包证据不视为本轮重跑。
