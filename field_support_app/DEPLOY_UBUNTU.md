# Ubuntu 桌面部署

适用于有 sudo 权限的普通桌面用户。以下命令以 Ubuntu 24.04 Desktop 为例；其他版本、ARM 工控机需先核对 Qt/PySide6 wheel 与系统库兼容性。

默认运行方式：后台 Core 随开机启动；浮窗在该用户登录图形桌面后启动。可在 App 设置页关闭这两项自启动，修改从下次开机生效，无需重新安装。也可以用本机浏览器代替浮窗。模型进程按分析请求启动。无需安装 lark-cli 或独立 Gateway，飞书使用 App ID / App Secret 直连。首次启动没有转人工任务时不建立飞书长连接；已有待处理人工任务时会恢复处理。

## .deb 快速安装

推荐使用提供的 `field-support-agent_0.1.0_all.deb`。先确保业务代码已在 Ubuntu 上，且该目录有 Git `origin`：

```bash
sudo apt install ./field-support-agent_0.1.0_all.deb
sudo field-support-setup --repo "$HOME/business/debug-agent-test"
```

第二条命令自动读取 `origin` 并生成绝对路径配置，安装 Python 依赖、Core 系统服务与浮窗登录项。仓库没有 `origin` 时加 `--git-url 'git@github.com:组织/仓库.git'`；使用 root 登录时另加 `--user 现场用户名`。安装完成后，仍需现场账号执行第 2 节的 Codex 安装与登录，并在 App 设置页填写模型和飞书信息。升级 `.deb` 后运行 `sudo field-support-setup --reuse`，已有问题数据及自启动选择会保留。

安装包本身不包含 Codex 登录凭证或 Linux Python 原生 wheel；首次 `apt install` 与 `field-support-setup` 需要联网。断网部署需要预先准备系统软件包、Python wheel 和 Codex CLI。移除 `.deb` 会停止并卸载服务，保留 `/var/lib/field-support` 中的问题记录。

以下章节保留源码安装步骤，以及首次验收和运维细节。

## 1. 安装系统依赖

在 Ubuntu 现场用户的终端执行，应用本身不要以 root 运行：

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git curl ca-certificates \
  procps iproute2 usbutils pciutils lsof ripgrep \
  libgl1 libegl1 libnss3 libxkbcommon-x11-0 libxcb-cursor0 \
  libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 \
  libxcb-xinerama0 libxcb-xkb1 libx11-xcb1 fonts-noto-cjk
```

Ubuntu 24.04 音频运行库：

```bash
sudo apt install -y libasound2t64
```

Ubuntu 22.04 对应使用 `libasound2`。Qt 依赖说明：https://doc.qt.io/qt-6/linux-requirements.html

## 2. 安装并登录 Codex

普通用户安装官方 Linux CLI，不复制 Mac 的二进制或虚拟环境：

```bash
curl -fsSL https://chatgpt.com/codex/install.sh -o /tmp/field-support-install-codex.sh
sh /tmp/field-support-install-codex.sh
```

按安装器提示重新打开终端，然后执行：

```bash
command -v codex
codex --version
mkdir -p "$HOME/.codex"
chmod 700 "$HOME/.codex"
nano "$HOME/.codex/config.toml"
```

在 config.toml 顶层设置以下项（已有同名项则修改，不重复添加）：

```toml
cli_auth_credentials_store = "file"
```

后台服务不能依赖已解锁的桌面钥匙串，因此使用本地文件保存登录状态。然后登录：

```bash
codex login
codex login status
codex exec --help
codex app-server --help
```

如浏览器登录回调不可用，可在账号支持时使用 `codex login --device-auth`。

当前适配器依赖 `exec --ignore-rules`、app-server 的 `thread/start`、`thread/resume`、`thread/delete` 和文本增量事件。Mac 上验证过的版本为 `0.154.0-alpha.6.2`；Ubuntu 所装版本需做下文端到端验证，不能假定所有旧版都支持。若缺少这些能力，使用官方提供的兼容 Linux 版本或更新适配器，不能放宽只读限制。

官方安装：https://developers.openai.com/codex/cli

官方认证：https://developers.openai.com/codex/auth

## 3. 放置应用和业务代码

将提供的源码压缩包放到 Ubuntu 的下载目录，在普通用户终端解压：

```bash
mkdir -p "$HOME/apps"
tar -xzf "$HOME/Downloads/field-support-app-ubuntu-source.tar.gz" -C "$HOME/apps"
cd "$HOME/apps/field_support_app"
```

正式业务仓库保留原位置，只需现场用户能读取。初次也可用模拟仓库验证：

```bash
mkdir -p "$HOME/business"
git clone git@github.com:brbzjl-test/debug-agent-test.git "$HOME/business/debug-agent-test"
```

私有仓库需要该 Ubuntu 用户已有 GitHub 访问权限。可以直接复制已准备好的业务目录，应用不会自动 clone、pull 或改动仓库。模拟仓库远端内容以实际 push 的版本为准。

生成配置；下面使用模拟仓库，部署真实业务时修改名称、Git 地址和绝对路径：

```bash
mkdir -p "$HOME/.config/field-support"
cat > "$HOME/.config/field-support/config.yaml" <<EOF
business_repositories:
  - name: debug-agent-test
    git_url: git@github.com:brbzjl-test/debug-agent-test.git
    local_path: $HOME/business/debug-agent-test
EOF
```

ROS 拓扑可选。业务日志目录可以在配置的 `log_paths` 中填写绝对路径；例如：

```yaml
log_paths:
  - /tmp/singel_workstation_runtime/A2机器人宝维塔NCU上下料
```

飞书、现场名称、设备名称、Codex 和管理密码稍后在设置页填写。保存现场名称和设备名称后，应用会自动创建或复用对应的现场支持群，创建群的 bot 会自动加入。

## 4. 安装后台与启动界面

在解压出的 field_support_app 目录执行：

```bash
sudo bash scripts/install.sh \
  --user "$(id -un)" \
  --config "$HOME/.config/field-support/config.yaml"
systemctl status field-support-core.service --no-pager
```

首次安装可用 `--autostart on` 或 `--autostart off` 指定初始状态。不传参数时首次安装默认 `on`；后续升级安装保留设置页里的选择。安装器会重建 Linux 虚拟环境，安装应用到 `/opt/field-support-agent`。开机时轻量启动门禁读取 `/var/lib/field-support/autostart.mode`：只有值为 `on` 时才启动 Core，并允许该次开机的图形登录启动浮窗。设置页切换会写入此文件，不会关闭当前运行的程序；下次开机才生效。Core 首次启动只做一次健康检查，没有定时健康检查任务；周期仍待确定。

关闭自启动后，手动启动 Core：

```bash
sudo systemctl start field-support-core.service
systemctl status field-support-core.service --no-pager
```

当前桌面不必注销，手动打开浮窗：

```bash
/opt/field-support-agent/.venv/bin/python \
  /opt/field-support-agent/scripts/run_ui.py \
  --runtime-dir /run/field-support
```

不使用浮窗时，在现场普通用户的终端启动连接同一个 Core 的网页服务：

```bash
/opt/field-support-agent/.venv/bin/python \
  /opt/field-support-agent/scripts/run_web.py \
  --runtime-dir /run/field-support --no-browser
```

然后在**同一台电脑**的浏览器打开 `http://127.0.0.1:8765/index.html`。省略 `--no-browser` 会尝试自动打开浏览器；端口占用时可加 `--port 8767` 并打开对应端口。网页服务只监听本机回环地址，终端需保持运行；`Ctrl+C` 只关闭网页入口，不停止 Core。请用安装时指定的普通用户运行，不能用 `sudo` 运行网页脚本。这个入口连接真实问题数据库，不是演示预览。

浮窗退出也只停止前端，后台 Core 继续运行；需停止整个应用时还要执行 `sudo systemctl stop field-support-core.service`。关闭自启动后，重启电脑需手动启动 Core 和所需界面，之后仍可在设置页重新开启。

不要同时启动 `field-support-agent serve --desktop`，它会创建另一套后台和数据目录。

## 5. 设置页配置

| 设置 | 填写内容 |
|---|---|
| 业务仓库 | Ubuntu 上实际存在的代码目录及 Git 地址 |
| 业务日志目录 | 每行填写一个绝对路径，例如 `/tmp/singel_workstation_runtime/A2机器人宝维塔NCU上下料` |
| Codex 程序 | `command -v codex` 输出的绝对路径 |
| Codex Home | `/home/实际用户名/.codex`，与登录使用的目录相同 |
| 模型 | 留空沿用 Codex 本机默认模型；也可填该账号实际可用的模型 |
| 推理强度 / 超时 | 初次沿用 Codex 本机默认强度 / 300 秒，再按验证情况调整 |
| 飞书连接方式 | App ID / App Secret 直连 |
| 现场名称 / 设备名称 | 用于自动创建或复用对应的现场支持群；创建群的 bot 会自动加入 |
| 飞书凭证与支持群 | App ID、App Secret、支持群 chat_id |
| 多维表格 | 对应 app_token 和 table_id |
| 管理密码 | 设置一个本地管理密码，用于历史记录删除 |
| 开机自启动 | 同时控制下次开机的 Core 和浮窗登录启动；当前会话不受影响 |

飞书应用应在支持群中，并已有消息、文件上传、卡片回调和目标多维表格的相关权限。复制凭证只通过本机设置页操作，不放进源码压缩包。

如果终端 Codex 能用、App 分析却未完成，先在设置页将“Codex 程序位置”改为 `command -v codex` 输出的绝对路径；模型和推理强度留空/选“跟随 Codex 默认”，让后台沿用同一份 Codex 配置。设置页保存后再提交一条现场现象。具体失败原因可在后台日志 `journalctl -u field-support-core.service -n 100 --no-pager` 和对应问题的 `/var/lib/field-support/issues/<问题ID>/analysis/latest.json` 的 `error` 字段查看；现场聊天只显示简化提示。

先点击凭证验证，再通过一次真实转人工检查群消息、证据附件、方案回传、多维表格更新；凭证验证成功不代表所有业务权限均已通过。

设置页更换业务目录后，systemd 的只读绑定不会自动更新。同步修改 config.yaml 并重新执行安装脚本，让 OS 层保护跟随新路径。

默认服务仅放行该用户 `~/.codex` 的写入。如果更换 Codex Home，需要相应修改服务的 ReadWritePaths，并确保该目录不在业务仓库内。

## 6. 首次验收

1. 新建问题并提交现象，确认生成问题 ID 和 snapshot。
2. 检查 snapshot manifest 中各项采集成功/失败的原因。
3. 确认 AI 可以读取业务代码并流式回答，再补充一轮消息检查续聊。
4. 选择转人工，检查飞书同一话题中的消息和证据 ZIP。
5. 工程师提交 solution 和验证方法，现场确认后检查状态及多维表格。
6. 用专门的测试问题验证工程模式删除，同时检查关联 Codex 会话被清理。
7. 设置页开启自启动时重启 Ubuntu，确认后台开机启动、图形登录后浮窗出现；关闭时确认均未自启，再手动启动 Core 和网页入口。

所有证据采集按当前普通用户可见范围执行。内核日志权限不足会记录为缺失；需要时由管理员授予日志读取权限，不必将整个应用改为 root。

如果使用 ROS，systemd 不会自动加载用户终端中 source 的 ROS 环境；当前部署默认只能收集普通 Linux 证据。需要 ROS 拓扑采集时，应另行配置服务的 ROS 环境后验证，不能仅以终端中 `ros2` 可运行作为依据。

## 7. 运维和位置

```bash
# 后台日志
journalctl -u field-support-core.service -n 100 --no-pager

# 修改程序或依赖后，重新安装部署包；只重启不会把新源码复制到 /opt
# 设置文件变动或日常故障恢复可重启后台
sudo systemctl restart field-support-core.service

# 重启后台会更新本机会话 token，已打开的浮窗或网页服务应退出后重新运行对应脚本

# 停止 / 启动后台
sudo systemctl stop field-support-core.service
sudo systemctl start field-support-core.service
```

| 内容 | 位置 |
|---|---|
| 安装程序 | `/opt/field-support-agent` |
| 问题数据库 | `/var/lib/field-support/core.sqlite3` |
| 设置及凭证 | `/var/lib/field-support/settings.json` |
| snapshot 和分析结果 | `/var/lib/field-support/issues/` |
| 飞书本地状态 | `/var/lib/field-support/feishu-state.json` |
| 本机 API 运行信息 | `/run/field-support/` |
| 自启动设置 | `/var/lib/field-support/autostart.mode`，`on` 或 `off`，可手动编辑 |
| Codex 会话及登录状态 | 现场用户的 `~/.codex/` |

浮窗的置顶、定位和拖动需要在实际桌面会话中验收。Wayland 下如果窗口管理限制导致体验不同，可用 Ubuntu 登录界面的 Xorg 会话比较验证，不关闭沙箱来解决 UI 问题。

当前已完成本机代码、打包和自动化检查，尚未在目标 Ubuntu 实机执行验收。离线 snapshot 可保存；断网后 AI 分析的持久自动重试仍待完善。
