# v0.8 独立性与可靠性审查

2026-09-30；本地分支 feat/v080-workbench；实现版本 0.8.0a3，尚未公开发布。human 明确要求 v0.8 从此完全独立。本轮没有合并 v0.7.6 的修补，也没有改在用 v0.7 checkout 或服务。

## 审查结论

保留已经形成的项目/员工/任务工作台，而移除旧邮箱运行链。项目数据、事务状态迁移、人工审批/验收和单 owner 锁可继续作为单机基础。设备层作为可选 transport，不增加分布式数据库、管理 LLM 或第二调度器。无需为了“架构升级”重写这些已有可靠模块。

独立并非另改产品名称或强迫普通用户部署服务器。产品继续叫 agent-mailbox，GitHub 保持版本线；默认一个本机进程和 loopback 浏览器。CLI/可选桌面启动器/无界面 node 使用同一产品域。

## 实际问题与处理

| 级别 | 可触发的问题 | 本轮处理与证据 |
| --- | --- | --- |
| P1 | alpha2 默认 console 与 Python 入口仍启动 v0.7 server，包内同时含两套产品 | 默认入口统一工作台；删除 17 个旧生产模块并重写旧 CLI；删除 39 个旧测试文件、7 个旧安装/验收脚本；包内容及隔离入口检查 |
| P1 | 远端执行已产出结果，但发送回执或最后一次 control 断网时，结果没有 durable 保存，重启只报中断 | 私有 terminal outbox 先保存；断线/重启重发原结果；不重复模型执行 |
| P1 | 主控已写终态但回包丢失，重复回执会被 invalid_state 拒绝 | SQLite 同事务存 run+canonical payload 摘要；同回执可确认，不改人已验收状态；冲突/旧 run 拒绝；本机清理写盘失败保留原回执，不能变成执行失败 |
| P1 | 邀请已消耗，配对回包或 client.json 保存失败使同设备无法重试 | 首次请求前持久 256-bit proof；server 仅 hash；同 invite/device/proof 恢复同 token；其他 proof/撤销拒绝 |
| P2 | 运行准备判断只校验部分 npm 包，遗漏受管 Codex/native host；模型查询可能读 PATH 中另一套 CLI | 统一 manifest 版本源，按实际 Node 架构核实 CLI/host；模型元数据使用同受管 CLI；不把 metadata 当执行成功 |
| P2 | ACP notification 排队可能晚于 permission callback，已发送的工具标题在审批界面缺失 | 仅同 run、同 tool ID 等待实际事件，最多 250ms；不同 run/工具不拼接，缺信息仍不补造、不自动授权 |
| P2 | 主控没运行组件，UI 误阻止远端员工派工；准备组件会丢已写任务草稿 | 本机/远端分别门控；更新运行组件区并保留目标、员工、模型和权限；真实隔离浏览器/API验证 |
| P2 | UI 每种工具仅能加入一个身份，暂停/退役员工误计入上手完成 | 可建多个名称，清楚显示共用原生账号；仅在岗员工计数，历史/退役守卫保留 |
| P2 | 缺 headless 节点入口；CGNAT 私网被拒绝，listener 非 loopback 时绑定所有接口 | node join/map/employee/run；允许明确 RFC1918/loopback/CGNAT；仅绑选定接口，固定 TLS pin 的显式 SSH dial override |
| P2 | 远端 started/event 改库后不唤醒 human UI；拒绝未读取 HTTP body 后继续复用连接 | 进度 notify；拒绝来源/非法 framing 后关连接，避免残留 body 污染后续请求 |
| P2 | CI 与发布复制测试矩阵，默认 bridge 依赖缺失会大量 skip | 一份复用 checks workflow，固定 Node 和 npm lock；发布只接 v0.8 tag，校验 metadata/tag；未运行 GitHub 新 CI |

旧模块在 v0.7 里有实际责任，不把它们称为“旧版无用代码”。它们对本独立产品不再是发行依赖。Git 历史和历史文档保留；删除旧测试不代表旧场景获新版本通过。

## 保留与不做

保留私有 SQLite、短事务、原子领取、单员工串行、显式中断和人工验收。local/remote 项目工具是不同传输边界，不能仅因为工具名称相同就合并掉权限检查。执行桥接器使用固定依赖及 CLI/host 配对，不回退不确定的全局实现。

本轮没有实现 universal app control、workflow DAG/拖拽、员工独立 provider 账号、独立编辑工作区、代码同步、自动更新/回滚、短时凭证、审计防篡改、完整成本管理或 AI ERP。工作历史/治理事件已存在，但不等同审计级日志。记忆用本地笔记和文本搜索，向量服务暂不需要。

香港/硅谷 Ubuntu 作为可选执行节点，通过既有私网或 SSH 隧道连接；主控必须开着。这里没有访问真实服务器、执行 SSH部署、安装 systemd 或进行跨国网络验收。

## 验证边界

macOS ARM64/Python 3.13.12/Node 22.23.1；受管 runtime package-lock 固定依赖。最终源码集 **197 passed, 1 skipped**（117.82s）；skip 为故障注入的依赖缺失场景在完整 runtime 环境中不适用，不是 Windows/Linux 实机结果。Ruff source/tests 与构建/检查脚本通过。

新回归覆盖：独立入口/prepare 锁；schema 4→5 保留身份与项目凭据；丢失配对响应/凭据落盘失败；exact receipt 在 human 已验收后重发；取消优先；终态发送前/提交后掉线重启；同进程恢复不重跑；主控确认后 active/outbox 清理写盘失败保留交付；CGNAT/选定接口；实际 node CLI、SIGTERM 清理 fake child、持久 SSH 地址供实际 stdio 项目工具使用。

浏览器有 8 组真实隔离 HTTP/SQLite 操作检查，consoleErrors 为空，见 [证据](../evidence/v080-independent/ui-results.json)。发现、登录、运行准备、模型及远端记录使用清楚标识的 fixture；没有本轮真实模型调用。已有旧 alpha provider 证据不能充当新版本物理网络和账户验收。

独立 wheel/app 的构建与启动、源码 bundle/checksum 记录在交付目录的 VERIFICATION.json 和 DELIVERY.md。macOS app 仅本机 ad-hoc seal，不是 Developer ID 签名或公证。新 CI 仅改定义，尚未推送运行。

AOCI rules/overview 返回原有 index_invalid/code_object_path_unresolved，本轮没有伪造认知/重建索引；按绑定源码及 CodeGraph 导航完成审查。收口 maintain 的实际返回记录在交付验证文件中。
