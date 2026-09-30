# 从 GitHub 上手 v0.8

当前是未公开发行的 0.8.0a7 alpha；获取这一版须用独立源码 checkout 或提供的本机构建产物。产品公开 PyPI 版本不代表本 alpha。

## 单机

Python 3.10+，执行工作需要 Node.js 22.13+。在 v0.8 checkout 建独立 venv，安装本目录：

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install .
agent-mailbox --home ~/.agent-mailbox-v08
```

Windows PowerShell 激活改用 `.venv\Scripts\Activate.ps1`。以上使用独立 v0.8 数据目录。使用自带运行组件的 macOS app 则双击启动；unsigned alpha 可能受 Gatekeeper 阻挡，GitHub 下载不消除系统校验。

在“员工”页发现并登记本机 app/CLI，再创建项目、从名册添加项目成员。普通消息用于同步；选择接收者并点“请求协作”才触发只读任务。必要时点“准备运行环境”。Codex/Claude 在这台电脑完成原生登录后，先派一个小任务验证交付、权限与人工验收。管理工作台不要求额外模型账号。准备组件会下载 package-lock 固定依赖，不修改全局 CLI 或登录。

CLI 可以在关闭工作台后显式准备同一个 home：

```sh
agent-mailbox prepare --home ~/.agent-mailbox-v08
agent-mailbox --home ~/.agent-mailbox-v08
```

同一 home 只允许一个工作台或执行节点 owner。退出按钮会停止应用，继续工作时重新运行原命令。`python -m agent_mailbox` 同样进入工作台。`agent-mailbox --version` 显示版本。

## 模型与认证检查

Codex 若返回 `MODEL_UNSUPPORTED`，新建任务时从服务实际提供的模型列表选择一个模型。工作台不会自动修改全局模型设置或换模型重跑。Claude 的原生 `auth status` 与受管 ACP 会话认证是不同检查；会话返回 `AUTH_REQUIRED` 时，需要完成该运行入口支持的认证，不能把原生已登录当成执行已验证。

## 更新与备份

停止应用，备份整个选定 home/workbench（包括 state.db、设备私钥和身份文件，备份应私有）。用新兼容 wheel 替换程序并使用原 home；项目和身份不因重新安装程序而重建。当前不支持自动下载安装、数据库降级或自动回滚。不要删除运行数据来“升级”。

可选服务器用 [无界面节点指南](NODES.md)。单机用户无需部署服务器、配置私网或为每位员工手动安装 MCP。
