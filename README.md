# Bianbu Cloud（gdriscv）用 API KEY 实现 “SSH-like” 远程 Shell（仅 18080）

这个方案的本质不是 TCP SSH 端口转发，而是：`gdriscv.com` 的 Remote API 用你的 `API KEY` 把 HTTP 请求转发到实例的 **18080** 端口。所以你需要在实例里启动一个监听 `18080` 的 HTTP 服务（本仓库提供了一个最小 agent），然后在本地用 PowerShell 脚本去调用 Remote API，实现：

- 远程执行命令（类似 `ssh host "cmd"`）
- 上传/写入文件（类似 `scp`/`rsync` 的最小子集）

## 安全提示（务必看）

`agent_server.py` 提供远程执行能力；请只在你控制的实例里使用：

- **不要把 `API KEY` 写进代码/README**；只放在 `secrets.env`（已加入 `.gitignore`）。
- 建议优先 `AGENT_HOST=127.0.0.1`（仅本机监听）。如果发现 Remote API 访问不到，再改成 `0.0.0.0`。
- 用完就停掉 agent（`Ctrl+C`），并尽量限制 `AGENT_BASE_DIR` 到你需要的目录。

## 目录说明

- `agent_server.py`：跑在实例里的 HTTP agent（`/health`、`/exec`、`/write`、`/ls`、`/read`、`/download`）。
- `call.ps1`：最底层调用器（负责带上 `X-API-KEY`，拼接 `deviceId`，发到 `gdriscv.com`）。
- `exec.ps1`：远程执行命令（默认用 `cmd_b64`，避免网关把 body 变形）。
- `write.ps1`：写入/上传文件（base64）。

## 1) 在实例里启动 agent（端口必须是 18080）

在 Bianbu Cloud 的调试终端/远程终端里，把 `agent_server.py` 上传到实例（比如放到 `~/agent_server.py`），然后运行：

```bash
cd ~
python3 --version
AGENT_HOST=127.0.0.1 AGENT_PORT=18080 AGENT_BASE_DIR="$HOME" python3 ~/agent_server.py
```

正常会看到类似输出：

```text
[agent] listening on http://127.0.0.1:18080 (base_dir=/home/bianbu)
```

## 2) 在本地（Windows）配置 API KEY + deviceId

复制配置模板并填入你的信息：

```powershell
Copy-Item .\secrets.env.example .\secrets.env
notepad .\secrets.env
```

格式：

```env
API_KEY=...
DEVICE_ID=...
```

## 3) 调用示例（PowerShell）

健康检查：

```powershell
.\call.ps1 -Path "/health"
```

远程执行：

```powershell
.\exec.ps1 -Cmd "uname -a"
```

写入文本：

```powershell
.\write.ps1 -RemotePath "hello.txt" -Text "hello from windows"
```

上传本地文件：

```powershell
.\write.ps1 -RemotePath "hello.py" -LocalFile ".\\hello.py"
.\exec.ps1 -Cmd "python3 hello.py"
```

### 解析返回值（拿到 stdout/stderr）

`gdriscv.com` 的 Remote API 会把 agent 的响应包一层，agent 的 JSON 在 `data.res` 里（是个字符串）。例如：

```powershell
$r = .\exec.ps1 -Cmd "pwd" | ConvertFrom-Json
$inner = $r.data.res | ConvertFrom-Json
$inner.stdout
```

## 常见坑 / 排错

- **只能用 18080**：平台限制端口只支持 `18080`。
- **HTTP 方法限制**：Remote API 对某些方法（例如 `PUT`）可能不支持；本仓库默认用 `GET/POST`。
- **启动了但本地访问不到**：把 `AGENT_HOST` 从 `127.0.0.1` 改成 `0.0.0.0` 试试。
- **命令里包含复杂字符**：优先用 `exec.ps1`（它走 `cmd_b64`），不要自己拼 `cmd=...`。

## 这能不能“真正 SSH”？

不行：Remote API 是把 HTTP 转到 18080，并不是给你一个可用的 TCP 22 端口，也没有交互式 TTY。它更像一个“带鉴权的 HTTP 远程执行/传文件通道”。如果你确实需要标准 SSH，一般要走 **反向连接/隧道**（实例主动连到你可访问的服务器）这一类方案。
