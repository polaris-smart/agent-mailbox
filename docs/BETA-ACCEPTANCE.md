# v0.8.0 Beta 2 验收边界

2026-10-01，本地版本 **0.8.0b2**。默认路径：发现员工 → 登记并检查能力 → 创建项目组 → 加入员工和资料 → 分配任务 → 邮件交接 → human 验收。基础闭环已验证；尚未公开发布 GitHub/PyPI。

| 范围 | 当前能力（含 Beta 1 基线验证） | 边界 |
| --- | --- | --- |
| 员工 | 发现、登记、登录状态与执行能力分开；Codex、Claude CLI 真实交接 | 可发现 App 不代表可自动控制；不是所有 agent 均已验证 |
| 项目与邮件 | 项目成员、收件箱、显式任务、审批、取消、验收 | 普通邮件不暗中创建任务；没有通用拖拽 DAG workflow 编辑器 |
| 资料 | 实时查看与不可变版本分开；员工提案由 owner 确认；任务领取时固定批准版本 | 文本提案不覆盖源文件；没有后台 Git 同步；历史内容可能因新增凭据脱敏而改变返回校验值，会明确标记 |
| 资料展示 | PRD、todo、daily update、架构资料；离线单文件 HTML 隔离预览 | 阻止外部网络、父页面和工作台 API；依赖 CDN 的报告需转为离线单文件 |
| 工作日志 | 从持久任务事件、邮件、笔记和资料审批生成，只读 | 不等于防篡改审计；内部 subagent 作者身份没有独立认证，沿用父员工会话归属 |
| 检索工具 | 可选 CodeGraph 本机 CLI 与已有索引；失败原因与新鲜度未知可见 | 不自动建立索引；Windows 适配器明确不支持；AOCI-Code 索引路径错误，完整治理未整合；Graft 未整合 |
| 跨设备 | Mac 协调端 → Ubuntu ARM64 容器节点，配对、HTTPS 身份校验、授权、映射、任务回执、版本校验、断线恢复、撤销 | 节点任务使用确定性测试执行器；Ubuntu 原生 LLM 适配器和真实 HK/US 服务器未验证 |
| UI | 左侧团队/当前项目/管理分组，项目选择器，中英文，手机抽屉与键盘操作，带样式的协议与帮助 | 其他语言未声明支持 |
| 安装升级 | Mac ARM64 独立 wheel、打包 App、本机保留数据升级与迁移备份 | App 无 Developer ID 签名/公证；手动升级，无自动更新器；Windows 发布产物未验证 |

## Beta 1 基线验证记录（继承能力，非 Beta 2 重跑）

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
