# 员工侧信箱验证 — 0.8.0a7

2026-09-30。本轮不调用 LLM，不推送、不公开发行。

## 行为

- `project_messages()` 默认绑定员工的收件，支持 `folder=sent/group`。本人的群广播计入发件，群视图展示其他员工与 human 的广播。
- 最新记录按时间正序返回，SQL 先筛身份和项目再取最近 1–100 条；`has_older` 明确提示历史未全部返回，未实现分页。
- 本地 MCP、远端配对工具一致；本地与远端受管启动上下文只附本人收发和群消息。
- 读取不 ACK、不派工、不改验收状态。human 全项目视图保留；项目共享任务与成果不变，因此不能把相关性过滤宣称为端到端私密邮箱。
- 受管工具入口不等于独立员工网页登录；未适配的 app 不具备自动执行能力。

## 验证

1. 全套 Python 测试：219 passed，41 runtime tests 因未指向依赖暂跳；随后用固定 `/tmp/v080-full-runtime` 单独跑 runtime suite：40 passed，1 skipped（已安装适配器的环境不满足缺适配器负例）。合计 259 passed，1 skipped，没有模型调用。
2. 新增/扩展测试覆盖身份与项目过滤、筛选先于条数限制、上下文不附他人定向消息、分类/数量错误、读取无数据副作用、真实 stdio MCP 和私有 TLS 远端工具。
3. 非 editable wheel 与冻结 app 分别启动、登记入口、加入项目、发送普通消息不创建任务、Apache/NoFox/原 MIT 声明、干净退出通过。
4. 冻结 app 的真实 stdio MCP 收件/发件/群消息及上下文协议检查通过，使用临时虚构员工与资料。
5. Ruff、git diff --check、冻结 app codesign --verify --deep --strict 通过。签名仍为本地 ad-hoc，非 Developer ID、公证或公开分发验收。
6. 本机 59969 预览升级至 a7；18 入口、1 项目、0 任务、0 消息，员工/项目/成员/任务/消息行 hash 均保持一致。SQLite 一致性备份保存在私有 home。没有真实员工协作任务，不能声称本轮验证了双员工模型协作。

构建与完整证据：`/Users/interia/tools/agent-mailbox-builds/v080-agent-inbox-alpha7/`。路线图为独立设计提案，不将其余未确认功能写成 PRD 已实现事实。
