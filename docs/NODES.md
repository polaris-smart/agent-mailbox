# 可选设备：LAN 与无界面 Ubuntu 节点

单机用户不用做这些步骤。此指南对应独立 0.8.0a3；本机隔离 HTTPS/子进程验证不代表真实 Ubuntu、LAN 或香港/硅谷服务器已经验收。

## 接入准备

主控是运行工作台的电脑，必须开着且可达。Ubuntu 是可选执行节点，不需要桌面 app 或 human HTTP 管理服务。节点须已有本 v0.8 wheel/源码安装、Python 3.10+、Node.js 22.13+、本机 agent 原生登录和项目 checkout。管理账号不作为 provider 登录使用。

节点安装到独立 venv，用节点自己的数据 home。先准备运行组件（会下载锁定 npm 依赖）：

```sh
agent-mailbox prepare --home ~/.agent-mailbox-node
```

主控在设备页面显式开启监听，选择本机私网 IP；支持 RFC1918、loopback 和 CGNAT 私有组网地址，监听只绑定所选 IP。确认工作台显示的设备 HTTPS 地址与证书指纹，不要使用 human 浏览器的 HTTP 地址。

由主控为具体项目生成邀请码 JSON，通过可信渠道交给节点。保存为自己账号所有、0600 的文件，勿贴到公开 issue 或 shell 命令参数里。

```sh
chmod 600 ~/agent-mailbox-invite.json
agent-mailbox node --home ~/.agent-mailbox-node join --invite ~/agent-mailbox-invite.json
agent-mailbox node --home ~/.agent-mailbox-node map --project PROJECT_ID --path ~/projects/my-project
agent-mailbox node --home ~/.agent-mailbox-node employee --project PROJECT_ID --name "Ubuntu Codex" --kind codex
agent-mailbox node --home ~/.agent-mailbox-node run
```

将 PROJECT_ID 换成 join 显示的授权项目编号，路径换成这个节点实际存在的目录。kind 可为 codex/claude。主控能给注册的员工派工。每个映射项目有本地路径；配对不传源码、不同步 Git、不复制 agent 登录。远端多个节点自己的账号和文件仍需各自管理。

Ctrl-C 或 SIGTERM 停止节点。重新运行 run 使用原配对身份与映射，无需重新发邀请码。首次配对回包丢失会保留私有恢复 proof；用同邀请码与地址重试。不要删除身份文件来修复网络问题。

## 外部服务器：SSH 隧道示例

若 Mac 是主控而 Ubuntu 可通过 SSH 登录，可在 Mac 建反向隧道，让 Ubuntu 的 loopback 端口转发到 Mac 的 **设备 HTTPS 监听**。示例将设备监听设为 127.0.0.1，FLEET_PORT 换成工作台显示端口：

```sh
ssh -N -o ExitOnForwardFailure=yes -R 127.0.0.1:8443:127.0.0.1:FLEET_PORT ubuntu@YOUR_SERVER
```

保留这个连接。在 Ubuntu 上配对时仅替换拨号地址，保留邀请码中的原证书指纹和授权项目：

```sh
agent-mailbox node --home ~/.agent-mailbox-node join --invite ~/agent-mailbox-invite.json --coordinator-url https://127.0.0.1:8443
```

覆盖后的地址会持久化供后续 run 和节点项目 MCP 子进程使用。已有身份更换拨号地址也可显式传 --coordinator-url，验证同主控身份与 TLS 指纹后才保存。服务端 SSH 配置须允许相应远端端口转发；没有隧道不代表要改用公网 human API。OpenSSH 的 -R/-N 语义见 [官方手册](https://man.openbsd.org/ssh.1)。

香港、硅谷两台服务器可分别建立各自 SSH 连接和节点配对，各自使用本机 8443；不能把未经实测的示例当部署成功。隧道和主控断线后，审批不会自动授权。终态回执已落节点本机时会重发原结果；执行状态不确定则标记中断，不能自动重复模型任务。

## 常驻运行

完成前台真实任务、审批和重启验收后，再按服务器已有运维方式托管 `agent-mailbox node --home ABSOLUTE_HOME run`。进程必须使用同一 OS 用户、原生登录环境、固定 home 和私网连接。当前不自动安装 systemd、远程 SSH 服务或网络组件。
