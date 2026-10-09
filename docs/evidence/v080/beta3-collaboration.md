# Beta 3 接入、工作区与交付验证

版本 0.8.0b3，2026-10-01。本地开发与验证，尚未公开 GitHub/PyPI 发布，未部署 HK/US。构建与验证保存在 `/Users/example/tools/agent-mailbox-builds/v080-beta3/`；本页区分原生执行、UI 场景和历史版本证据。

## 当前已确认

- Mac 最终完整自动回归：355 passed、1 skipped，0 failed（141.62 秒）；另 20 项工作区/接入/接缝定向检查通过。Ruff、格式、JS 语法和 diff 检查通过。
- UI：27 项检查、0 页面异常；其中原 20 项流程覆盖接入检查/验证入口、固定交付、验收与合入分离、退回关联任务、中英文和移动视图；另 7 项模型 payload/元数据场景未执行模型。
- 工作区真实 Git 集成：并发独立 worktree、原目录不变、提交与未跟踪文件固定、冻结后修改不影响合入、干净/根目录/旧基线拒绝、退回 patch 继承及路径/符号链接/二进制/凭据边界。
- 项目同事交付通过授权工具读取固定记录，不读取任意源码/凭据文件；当前项目 MCP 共 11 工具。

## 原生协作与发行包

- 原生 Codex/Claude Beta 3 协作已通过，证据 `v080-beta3/native-collaboration.json` 为 passed:true：两员工 probe 均有真实 context/note 回执；Codex 显式选用兼容模型 独立修改，原目录保持不变；Claude 通过 project_delivery 读取固定 diff 并写笔记审查；Human 验收不改源文件，显式 apply 后精确标记合入。4 个任务均实际进入 review，再由验证操作者作为 owner 验收。
- 初次默认模型 MODEL_UNSUPPORTED 另存 `native-default-model-failure.json`；不是产品自动换模型，失败未伪装成功。
- wheel 和 macOS ARM64 App 均从独立数据目录启动，版本、协议版权、全局登记/入组、普通消息不触发任务、识别安装方式和正常退出通过；包验证没有调用模型。
- 冻结 App 的 MCP：11 个工具；固定资料读取、显式文本提案、真实 context/note probe 回执及 project_delivery 读取通过。没有员工审批/合入工具。
- 本机同 home 从 b2 升级至 b3：18 位员工、1 项目，业务表逐行 hash 保留；先完成一致性私有备份，schema 8→9 另有 0600 私有迁移备份。升级未派任务、未调用模型，接单保持可用。
- 本机 App 的新接入 GET 实测返回 8 项检查，未创建任务、未执行模型生成、接单未暂停；项目 probe 尚未在用户项目运行。
- 本机 App 页面另有 10 项检查通过，0 页面异常：中英文选择与持久偏好、协议样式、GitHub 链接、更新区及移动端不溢出。
- 发行 App 仅本地 ad-hoc 签名，没有 Apple Developer ID 和公证；构建不等于 macOS 公开发行信任已解决。
- Windows/Linux 实体设备、远端原生修改隔离与真实 HK/US 未验证。Beta 1 的 Ubuntu Docker 协议证据仍是历史基础版本，不当作 Beta 3 实机验收。

## 验证中发现的问题

第一次默认模型配置被执行组件拒绝，保留 `native-default-model-failure.json`。新增可选模型选择后，由操作者明确指定执行组件实际提供的模型；不会静默改原生配置或自动重跑。员工卡片的登录为“上次记录”，接入弹窗为“本次检查”，查看状态不伪装成已验证。

最终回归的一次运行出现原有多入口登录 fixture 断言失败（354 passed、1 failed、1 skipped），单独复测通过；保留 `tests-first-final-attempt.*`。随后完整重跑 355 passed、1 skipped，0 failed；没有改断言或放宽通过条件。

AOCI 本轮规则/overview 均返回 `index_invalid: code_object_path_unresolved`，未宣称认知索引通过，最终 maintain 结果独立保存。

## 产品边界

显式 probe 会消耗原生模型额度，成功须实际绑定上下文和随机笔记回执及正常结束。查看接入状态不调用模型。远端 probe 不支持；发现 App 不代表已有自动适配器。同设备员工默认共享原生登录。

本机修改任务要求干净且有提交的 Git 根目录，使用 home/task-workspaces/<task-id>；不是 OS 沙箱。交付固定文本 patch；员工报告不代表系统跑过测试。验收不自动合入；显式合入校验原基线、干净状态与固定内容，不 commit/push。退回任务仅在基线一致时继承上一轮 patch。远端 workspace-write 暂拒绝，只读通信保留。工作区不自动清理。

更新备份包含一致性 SQLite 和工作台身份/配置，不含 home/task-workspaces、外部 Git 或供应商登录；代码及保留工作区需要独立备份。二进制、符号链接、凭据、不安全或过大 patch 不自动合入。
