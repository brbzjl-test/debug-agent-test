# Debug Agent Test

这是现场调试助手的集成验证仓库，同时包含一个可控的业务程序模拟器。业务程序负责产生进程、日志和运行状态；现场调试助手负责只读采集、Codex 分析、问题流转和飞书协作。

仓库中的现场调试助手代码已经从原机器人业务仓库中独立出来。业务模拟器和调试助手可以分别运行，也可以把本仓库作为现场调试助手的只读业务仓库进行联调。

## 能力概览

- 用业务模拟器复现正常运行、设备缺失、软件冲突、依赖缺失、间歇性故障、进程崩溃和静默卡死。
- 从业务仓库、ROS 日志和配置的业务日志目录收集受限大小的证据文件。
- 使用本机 Codex CLI 进行只读分析，禁止写入业务仓库。
- 在本地 UI 中创建问题、追加现场信息、查看 AI 分析、转交工程师和确认解决方案。
- 通过飞书 App 或 `lark-cli` 向现场支持群发送问题卡片，并自动创建或复用现场群。
- 通过独立 Gateway 保存设备问题状态、处理设备鉴权、飞书回调、解决方案版本和多维表格投影。
- 通过 Feishu bridge 将飞书机器人消息连接到本机 Codex，支持问题编号、子问题、验证和历史同步。

## 目录结构

```text
.
├── app.py                         # 业务程序模拟器
├── tests/                         # 模拟器测试
├── field_support_app/             # 现场调试助手本地应用
│   ├── src/field_support_agent/   # Core、采集器、Codex、飞书和本地 UI
│   ├── scripts/                   # 本地运行、Ubuntu 部署和打包脚本
│   ├── deploy/                    # systemd 和桌面启动模板
│   └── tests/                     # app 测试
├── field_support_gateway/         # 服务端 Gateway
├── tools/feishu_codex_bridge/     # 飞书机器人到本机 Codex 的 bridge
└── docs/field-support/            # 需求、用户流程和设计文档
```

`logs/`、`runtime/`、虚拟环境、构建目录和本地凭据不会进入 Git。`tools/feishu_codex_bridge/config.local.json` 只允许保存在本机，不要提交。

## 环境要求

- Python 3.9 或更高版本。
- macOS 或 Linux。
- 运行本地 Codex 功能时，需要在运行账号下完成 Codex CLI 登录。
- 运行 Feishu bridge 时，需要已登录的 `lark-cli` 和可用的飞书机器人 profile。
- 本地 UI 桌面浮窗需要额外安装 PySide6；只使用浏览器 UI 时不需要。
- Gateway 只使用 Python 标准库；现场 app 的 Feishu 直连模式需要 `lark-oapi`。

## 快速开始：业务模拟器

业务模拟器不依赖第三方 Python 包，可直接运行：

```bash
python3 app.py --scenario normal --duration 5
```

常用场景：

| 场景 | 行为 | 预期用途 |
| --- | --- | --- |
| `normal` | 周期输出健康心跳并正常退出 | 验证正常采集 |
| `device_missing` | 启动阶段报告设备不存在 | 验证启动故障 |
| `software_conflict` | 报告控制权冲突和输入被拒绝 | 验证业务冲突 |
| `intermittent` | 短暂故障后恢复 | 验证日志中的历史证据 |
| `dependency_error` | 报告运行依赖缺失 | 验证依赖故障 |
| `process_crash` | 缓冲区过小时崩溃 | 验证参数修复前后差异 |
| `silent_hang` | 进程继续运行但停止心跳 | 验证静默卡死检测 |

例如验证 `process_crash` 的参数修复：

```bash
# 默认 32 MiB，不满足模拟程序需要的 48 MiB
python3 app.py --scenario process_crash --duration 300

# 提高参数后正常运行
python3 app.py --scenario process_crash --frame-buffer-mb 64 --duration 5
```

默认日志写入 `logs/business.log`，运行状态写入 `runtime/status.json`。可以通过 `--log-dir` 和 `--runtime-dir` 指定路径：

```bash
python3 app.py \
  --scenario intermittent \
  --duration 10 \
  --log-dir /tmp/debug-agent-logs \
  --runtime-dir /tmp/debug-agent-runtime
```

## 本地运行现场调试助手

### 安装开发依赖

```bash
cd field_support_app
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install -e .
```

如果需要桌面浮窗：

```bash
.venv/bin/python -m pip install -e '.[desktop]'
```

### 配置业务仓库

编辑 `field_support_app/config.example.yaml`，把 `local_path` 改成现场实际存在的业务仓库绝对路径。日志目录可以配置多个，每行一个：

```yaml
business_repositories:
  - name: debug-agent-test
    git_url: git@github.com:brbzjl-test/debug-agent-test.git
    local_path: /Users/brb/项目/debug-agent-test

log_paths:
  - /tmp/singel_workstation_runtime/A2机器人宝维塔NCU上下料
```

`log_paths` 只允许绝对路径。采集器会从这些目录中读取符合日志模式的最新文件，并对每个文件限制读取大小；不会修改业务目录。

### 启动浏览器 UI

```bash
cd field_support_app
PYTHONPATH=src .venv/bin/python -m field_support_agent.app \
  serve \
  --config config.example.yaml \
  --browser
```

启动后打开终端输出的本机地址。也可以使用开发脚本：

```bash
./scripts/dev_core.sh
./scripts/dev_ui.sh
```

设置页可以配置：

- 业务仓库和 ROS 拓扑文件。
- 业务日志目录，每行一个绝对路径。
- Codex 程序位置、Codex Home、模型、推理强度和超时时间。
- 飞书 App ID / App Secret，或本机 `lark-cli` profile。
- 现场名称和设备名称。
- 支持群 Chat ID、多维表格 App Token 和 Table ID。

保存飞书和现场配置后，应用会创建或复用以下命名的群：

```text
现场支持·现场名称·设备名称
```

如果只填写设备名称，群名会使用 `现场支持·设备名称`。创建群的 bot 会自动成为群成员；生成的 Chat ID 会写入本地设置，后续启动会复用已有群。

### Ubuntu 安装包

在仓库根目录构建 `.deb`：

```bash
python3 field_support_app/scripts/build_deb.py --output dist
```

安装并配置：

```bash
sudo apt install ./dist/field-support-agent_0.1.0_all.deb
sudo field-support-setup --repo /absolute/path/to/business-repository
```

安装脚本会创建 Core systemd 服务和可选桌面浮窗。详细的用户、SSH、服务、升级和排错说明见 [`field_support_app/DEPLOY_UBUNTU.md`](field_support_app/DEPLOY_UBUNTU.md)。

## Gateway 服务

Gateway 负责服务端设备鉴权、问题状态、幂等请求、Feishu 回调、解决方案版本和 Outbox 投影。它不在现场设备上保存 bot 凭据。

启动 Gateway：

```bash
cd field_support_gateway
FIELD_SUPPORT_DB=/var/lib/field-support-gateway/gateway.db \
FIELD_SUPPORT_DEVICES='{"station-1":"replace-with-random-token"}' \
FIELD_SUPPORT_ENGINEERS='["ou_engineer"]' \
FIELD_SUPPORT_CALLBACK_SECRET='replace-with-callback-secret' \
PYTHONPATH=src python3 -m field_support_gateway.server
```

启用 Feishu 投影和卡片事件时增加：

```text
FIELD_SUPPORT_LARK_PROFILE=inventory-bot
FIELD_SUPPORT_SUPPORT_CHAT_ID=oc_xxx
FIELD_SUPPORT_LARK_CLI=/usr/local/bin/lark-cli
FIELD_SUPPORT_BASE_TOKEN=basexxx
FIELD_SUPPORT_BASE_TABLE_ID=tblxxx
FIELD_SUPPORT_LARK_CARD_EVENTS=1
```

主要接口：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/v1/handoffs` | 设备创建或重试人工转交 |
| `GET` | `/v1/issues/{issue_id}/sync?after_seq=N` | 设备同步问题状态和事件 |
| `POST` | `/v1/issues/{issue_id}/confirm` | 现场确认解决方案 |
| `POST` | `/v1/issues/{issue_id}/verification-failure` | 现场验证失败并重新打开问题 |
| `POST` | `/v1/feishu/card-actions` | 接收签名后的卡片回调 |
| `POST` | `/v1/feishu/events` | 接收签名后的飞书事件 |

请求契约见 [`field_support_gateway/contracts.md`](field_support_gateway/contracts.md)。设备请求需要 `Authorization: Bearer ...`，创建转交和状态变更需要稳定的 `Idempotency-Key`。

## Feishu Codex Bridge

Bridge 用于另一种交互模式：飞书机器人长连接接收消息，本机 Codex CLI 处理只读分析，再由机器人把结果发回飞书。它与现场 app 的本地 UI 是独立入口，但可以共用同一个业务仓库。

首次配置和检查：

```bash
python3 tools/feishu_codex_bridge/bridge.py init
python3 tools/feishu_codex_bridge/bridge.py doctor
```

运行方式：

```bash
# 后台运行
python3 tools/feishu_codex_bridge/bridge.py start
python3 tools/feishu_codex_bridge/bridge.py status
python3 tools/feishu_codex_bridge/bridge.py stop

# 前台运行，Ctrl+C 停止
python3 tools/feishu_codex_bridge/bridge.py run
```

可选命令：

```bash
python3 tools/feishu_codex_bridge/bridge.py smoke-test
python3 tools/feishu_codex_bridge/bridge.py issues
python3 tools/feishu_codex_bridge/bridge.py issue ISS-20260911-XXXXXXXX
```

`init` 生成的 `config.local.json` 包含 profile、允许用户、允许群、工作目录和多维表格配置。它已被忽略，不能提交到 Git。模板见 [`tools/feishu_codex_bridge/config.example.json`](tools/feishu_codex_bridge/config.example.json)。完整的飞书权限和问题流转说明见 [`tools/feishu_codex_bridge/README.md`](tools/feishu_codex_bridge/README.md)。

## 问题流转

现场 app 和 Gateway 共同遵循以下状态：

```text
open -> pending_verification -> closed
                         \
                          -> open  (现场验证失败)
```

典型流程：

1. 现场人员创建问题并描述现象。
2. app 收集系统信息、业务状态、配置日志和 ROS 信息。
3. Codex 只读分析证据并给出排查建议。
4. 现场人员请求工程师支持，问题卡片和证据包进入飞书支持群。
5. 工程师提交解决方案和验证方法，状态变为 `pending_verification`。
6. 原现场上报人确认解决，状态变为 `closed`；验证失败则回到 `open`。

每个根问题有独立的 Codex 会话和飞书话题。复发问题创建独立 sub-ID，同时保留父问题关联关系。

## 安全边界

- 业务仓库始终按只读方式使用；配置、用户输入和 Codex 输出不能授权写入业务目录。
- 采集器只读取配置的仓库和日志路径，并限制单文件大小、文件数量和命令执行时间。
- 本地设置文件使用 `0600` 权限；飞书 App Secret 不通过本地 API 返回。
- Gateway 使用设备 token、回调签名和工程师白名单进行鉴权。
- Feishu bridge 的本地配置、token、App Secret、Base token 和 profile 不进入 Git。
- 不要把 `config.local.json`、`settings.json`、数据库、日志或 `.env` 文件上传到远端。

## 测试

业务模拟器：

```bash
python3 -m unittest discover -s tests -v
```

现场调试助手：

```bash
cd field_support_app
PYTHONPATH=src python3 -m unittest discover -s tests -v
node --test tests/test_ui_streaming.js
```

Gateway：

```bash
cd field_support_gateway
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Feishu bridge：

```bash
cd tools/feishu_codex_bridge
python3 -m unittest discover -s . -p 'test_*.py' -v
```

当前迁移版本已验证通过：业务模拟器 4 项、现场调试助手 135 项、Gateway 15 项、Feishu bridge 44 项，前端 Node 测试 6 项。

## 常见问题

### app 分析没有完成

先确认运行账号可以在终端直接执行 Codex，再检查设置页的 Codex 程序位置和 Codex Home。Ubuntu 服务日志：

```bash
journalctl -u field-support-core.service -n 100 --no-pager
```

对应问题的分析错误在：

```text
/var/lib/field-support/issues/<问题ID>/analysis/latest.json
```

### 日志没有被采集

确认日志目录是绝对路径，运行账号对目录有读取权限，并且日志文件匹配 `*.log`、`logs/*.log`、`log/*.log` 或 `**/latest/*.log`。目录不存在时不会阻断整次快照，但该目录不会产生证据项。

### 保存飞书配置后没有自动建群

确认 App ID / App Secret 或 `lark-cli profile` 可用，并填写至少一个现场名称或设备名称。应用需要能列出和创建飞书群；如果配置了已有 Chat ID，也会优先使用已有群。

### Gateway 请求返回 401

检查设备请求的 `Authorization` 是否为 `Bearer <device-token>`，并确认该 token 已在 `FIELD_SUPPORT_DEVICES` 中注册。不要把 token 写入仓库文件。

## 相关文档

- [`field_support_app/README.md`](field_support_app/README.md)：本地 app 说明。
- [`field_support_app/DEPLOY_UBUNTU.md`](field_support_app/DEPLOY_UBUNTU.md)：Ubuntu 安装和运维。
- [`field_support_gateway/README.md`](field_support_gateway/README.md)：Gateway 运行说明。
- [`field_support_gateway/contracts.md`](field_support_gateway/contracts.md)：Gateway API 契约。
- [`tools/feishu_codex_bridge/README.md`](tools/feishu_codex_bridge/README.md)：Bridge 权限、命令和问题台账同步。
- [`docs/field-support/software-design-requirements.md`](docs/field-support/software-design-requirements.md)：软件设计需求。
- [`docs/field-support/user-journey.md`](docs/field-support/user-journey.md)：用户流程。
- [`docs/field-support/implementation-plan.md`](docs/field-support/implementation-plan.md)：实现计划。
