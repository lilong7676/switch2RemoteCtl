# switch2RemoteCtl

**[English](#english)** | [中文](#中文)

## English

Control a Nintendo Switch 2 from a Raspberry Pi 5 by emulating a Switch Pro
Controller with [nuxbt](https://github.com/hannahbee91/nuxbt). The project is
aimed at repetitive automation: shiny hunting, material farming, scripted input
sequences, and similar tasks.

> Status: validated on a physical Switch 2. Initial pairing, web UI input, and
> reconnection after sleep/wake all work. The Pi still has to re-pair after a
> reboot because `enable` rotates the Bluetooth MAC (see below).
>
> Test log: [`docs/RESULTS.md`](docs/RESULTS.md) · Plan:
> [`docs/PLAN.md`](docs/PLAN.md).

### Repository layout

```
switch2RemoteCtl/
├── docs/               # plan and test log
├── scripts/
│   ├── switchctl.sh    # main entry point (most used)
│   ├── test_link.py    # connection / input stability test
│   └── apply_patches.sh# re-apply local patches after reinstalling nuxbt
├── macros/             # macro files
├── patches/            # standalone patch archive and originals
├── logs/               # runtime logs, last_mac
├── nuxbt/              # upstream repo as a git submodule
└── .venv/              # Python virtual environment
```

### Common commands

Run everything from the project root:

```bash
cd ~/switch2RemoteCtl
```

#### 1. After a Pi reboot: enable the nuxbt BlueZ override

```bash
./scripts/switchctl.sh enable
```

This asks for sudo. It writes
`/run/systemd/system/bluetooth.service.d/nuxbt.conf`, grants the system Python
raw-socket capabilities, **sets a new random Nintendo-OUI Bluetooth MAC**,
restarts Bluetooth, and brings `hci0` up. Run it after **every** Pi reboot
because `/run` is a tmpfs.

> Why rotate the MAC: the Switch 2 stores its side of the link key, but BlueZ on
> the Pi reports `Bonded: no` and does not persist it. Reusing the same MAC makes
> the console authenticate with the stale key, which ends in
> `Authentication Failure` and a UI stuck on `connecting`. A new MAC makes the
> console treat the adapter as a brand-new controller and pair from scratch.

#### 2. Connection test (most common)

1. On the Switch 2, open **Controllers → Change Grip/Order**.
2. On the Pi:

```bash
./scripts/switchctl.sh test
```

The script waits for the console, then presses **DPAD_RIGHT** every two seconds
for 120 seconds. Once the cursor starts moving, press **A once** on the console
to leave the menu, then stay on the HOME menu and watch for disconnects.

> Only one `test` instance may run at a time; the script locks
> `logs/test.lock`. If it says a test is already running, stop the old one with
> `./scripts/switchctl.sh stop` or `./scripts/switchctl.sh reset`.

Useful variants:

```bash
# only 30 seconds
./scripts/switchctl.sh test --seconds 30

# press A every second
./scripts/switchctl.sh test --button A --interval 1

# wait up to 600 seconds for the console
./scripts/switchctl.sh test --timeout 600
```

#### 3. Status

```bash
./scripts/switchctl.sh status
```

Shows the nuxbt version, plugin state, `hci0`, BlueZ adapter state, known
Switch devices, the last connection MAC and pairing state, running processes,
and the newest logs.

#### 4. Stop, reset, and rotate the MAC

```bash
./scripts/switchctl.sh stop      # stop test_link / nuxbt
./scripts/switchctl.sh reset     # clean stale processes + restart Bluetooth + hci0 up
./scripts/switchctl.sh newmac    # generate a new random Nintendo-OUI MAC
```

If the console cannot find the controller, or the connection is stuck after an
abnormal exit or sleep, run `reset` first. It cleans up with
SIGINT → SIGTERM → SIGKILL, restarts Bluetooth, brings `hci0` up, and prints the
status.

If the page stays on `connecting` and `btmon` shows `Authentication Failure`,
the console is using a stale link key. Stop the webapp/test, rotate the MAC, and
pair again:

```bash
./scripts/switchctl.sh stop
./scripts/switchctl.sh newmac
./scripts/switchctl.sh webapp
```

`enable` also rotates the MAC, so a normal Pi reboot usually avoids this
problem.

#### 5. Logs

```bash
./scripts/switchctl.sh logs                 # newest .out
./scripts/switchctl.sh logs test_link2.out  # specific file
```

#### 6. Run a macro

```bash
./scripts/switchctl.sh macro macros/example.txt
```

`macros/example.txt` uses the normal nuxbt macro syntax:

```
B 0.1s
0.5s
A 0.1s
```

A macro string can also be passed directly:

```bash
./scripts/switchctl.sh macro "B 0.1s
0.5s"
```

#### 7. Web UI

```bash
./scripts/switchctl.sh webapp
```

It listens on `0.0.0.0:8000` by default. Open:

- locally: `http://127.0.0.1:8000`
- on the LAN: `http://<PI_IP>:8000`

For local-only access (recommended, together with an SSH tunnel):

```bash
./scripts/switchctl.sh webapp -i 127.0.0.1 -p 8000
```

Then create a tunnel from your computer:

```bash
ssh -L 8000:127.0.0.1:8000 <USER>@<PI_IP>
```

and open `http://127.0.0.1:8000`.

Notes:

- **The webapp and `test` cannot run at the same time.** One adapter can host
  one controller; run `stop` first.
- The page has **no authentication**. Do not expose it to the internet.
- The browser Gamepad API requires HTTPS or localhost; over plain LAN HTTP, use
  the keyboard or macros.
- “Create Pro Controller” needs the Switch 2 on Change Grip/Order for the first
  pairing, exactly like `test`.

#### 8. Capture Bluetooth logs

```bash
./scripts/switchctl.sh btmon 20
```

Captures 20 seconds of HCI traffic to `logs/btmon-*.log` and prints key events
such as Connect, Disconnect, and Link Key.

### Connection flow

```
Pi reboot
  └─ ./scripts/switchctl.sh enable
       └─ Switch 2: Controllers -> Change Grip/Order
            └─ ./scripts/switchctl.sh test
                 └─ after it connects, press A once to leave the menu
                      └─ keep the controller process alive (macro/script)
```

### Known limitations (measured)

1. **The Switch 2 stores the link key; the Pi does not.** While connected,
   BlueZ reports `Paired: yes, Bonded: no`. Reconnecting with the same MAC fails
   with `Authentication Failure`; use `./scripts/switchctl.sh newmac` and pair
   again. `nuxbt macro -r` is effectively unusable with the Switch 2.
2. **Do not run `nuxbt demo`.** It is a blind navigation macro written for the
   Switch 1 menu layout; on the Switch 2 it can select Sleep.
3. **One adapter emulates one controller.** The Pi 5 onboard Bluetooth shares an
   antenna with Wi-Fi; prefer Ethernet.
4. **The connection is stable while held:** 120 seconds / 53 inputs without a
   drop in testing. Longer runs are still being evaluated.
5. The nuxbt override lives in `/run`; re-run `enable` after every Pi reboot.
6. **Sleep/wake:** the controller no longer crashes when the console sleeps;
   it transitions to `connecting`/`reconnecting` and recovers when the console
   reconnects. If it stays on `connecting`, run `newmac` + `webapp` and pair
   again. Keeping the controller process alive or disabling console auto-sleep
   also helps.
7. **Stale processes hold the adapter.** After an abnormal exit, run
   `./scripts/switchctl.sh reset`.
8. **Do not restart Bluetooth while the webapp is running.** It breaks the
   webapp's D-Bus connection: the UI can create a controller but stays on
   `connecting`. Always do `reset`/`newmac` first, then start the webapp.

### Local nuxbt checkout (git submodule)

Upstream nuxbt is included as a submodule at `nuxbt/`:

- fork: https://github.com/lilong7676/nuxbt
- work branch: `switch2-local`
- base: `v3.3.7`
- local commit: `9d5e08f` (`Local Switch 2 compatibility fixes`)
- switch 2 PR branch: `switch2-fixes`
- upstream PR: https://github.com/hannahbee91/nuxbt/pull/78
- change notes: `nuxbt/LOCAL_CHANGES.md`

Included changes:

- CLI `-a/--address` parsed as text instead of a boolean flag
- controller discoverability no longer times out after 180 seconds
- webapp does not auto-reconnect when the device is unbonded (Switch 2)
- webapp Socket.IO uses polling only
- recover from `ConnectionResetError` when the console sleeps instead of crashing the controller
- use the console peer address after a fallback connect
- built `web/static/dist` is force-committed (no Node.js on the Pi)

The venv uses an **editable install** of the submodule:

```
nuxbt.__file__ = <project>/nuxbt/nuxbt/__init__.py
Editable project location: <project>/nuxbt
```

Edits under `nuxbt/` take effect after restarting the webapp, and can be
committed and pushed to the fork directly. To reinstall:

```bash
./scripts/switchctl.sh stop
./scripts/switchctl.sh install
./scripts/switchctl.sh webapp
```

`./scripts/apply_patches.sh` now targets `nuxbt/nuxbt` (the submodule) by
default instead of site-packages. `patches/` remains as a standalone backup.

---

## 中文

用 Raspberry Pi 5 + [nuxbt](https://github.com/hannahbee91/nuxbt) 模拟 Switch
Pro Controller，控制 Nintendo Switch 2 做重复性操作自动化（刷闪、刷素材、
固定输入序列等）。

> 当前状态：已在实体 Switch 2 上验证。首次配对、Web UI 输入、休眠唤醒后重连
> 都正常。Pi 重启后仍需要重新配对，因为 `enable` 会更换蓝牙 MAC（见下文）。
>
> 实测记录：[`docs/RESULTS.md`](docs/RESULTS.md)；计划：
> [`docs/PLAN.md`](docs/PLAN.md)。

### 目录结构

```
switch2RemoteCtl/
├── docs/               # 计划与实测记录
├── scripts/
│   ├── switchctl.sh    # 统一调试入口（最常用）
│   ├── test_link.py    # 连接/输入稳定性测试
│   └── apply_patches.sh# 重装 nuxbt 后恢复本地 patch
├── macros/             # 宏文件
├── patches/            # 独立 patch 备份与原始文件
├── logs/               # 运行日志、last_mac
├── nuxbt/              # 上游仓库（git submodule）
└── .venv/              # Python 虚拟环境
```

### 最常用命令

所有命令都在项目根目录执行：

```bash
cd ~/switch2RemoteCtl
```

#### 1. Pi 重启后：启用 nuxbt 的 BlueZ override

```bash
./scripts/switchctl.sh enable
```

会要求 sudo 密码。它做四件事：写
`/run/systemd/system/bluetooth.service.d/nuxbt.conf`、给系统 Python 加
raw socket capability、**设置一个新的随机 Nintendo OUI 蓝牙 MAC**、重启
Bluetooth 并拉起 `hci0`。**Pi 每次重启后都要执行一次**（`/run` 是 tmpfs）。

> 为什么每次换 MAC：Switch 2 会保存手柄的 link key，但 Pi 侧不保存
> （`Bonded: no`）。继续用旧 MAC 时，S2 会拿旧密钥认证，结果就是
> `Authentication Failure`，页面一直 `connecting`。换新 MAC 让 S2 把它当成
> 新手柄重新配对。

#### 2. 连接测试（最常用）

1. Switch 2 进入 **Controllers → Change Grip/Order**
2. Pi 上执行：

```bash
./scripts/switchctl.sh test
```

脚本会等待 Switch 连接；连上后每 2 秒按一次右方向键，默认持续 120 秒。
看到光标开始移动后，在 Switch 上**只按一次 A** 退出菜单，然后在主菜单停留观察。

> 同一时间只允许一个 `test` 实例：脚本用 `logs/test.lock` 加锁。如果提示
> “已有测试在运行”，说明还有一个实例没退出，先执行
> `./scripts/switchctl.sh stop` 或 `./scripts/switchctl.sh reset`。

常用变体：

```bash
# 只测 30 秒
./scripts/switchctl.sh test --seconds 30

# 改按 A 键、每 1 秒一次
./scripts/switchctl.sh test --button A --interval 1

# 等待连接最多 600 秒
./scripts/switchctl.sh test --timeout 600
```

#### 3. 查看状态

```bash
./scripts/switchctl.sh status
```

输出包括 nuxbt 版本、plugin 状态、`hci0`、BlueZ 适配器状态、已记录的
Switch、上次连接的 MAC 与配对状态、正在运行的进程、最新日志。

#### 4. 停止、reset 与换 MAC

```bash
./scripts/switchctl.sh stop      # 停止 test_link / nuxbt
./scripts/switchctl.sh reset     # 清理残留进程 + 重启蓝牙 + 拉起 hci0
./scripts/switchctl.sh newmac    # 生成随机 Nintendo OUI MAC
```

如果 Switch 搜不到手柄，或异常退出/休眠后连不上，先执行 `reset`。它会用
SIGINT → SIGTERM → SIGKILL 清理残留的 `test_link.py` / `nuxbt` 进程，重启
Bluetooth，拉起 `hci0`，然后打印状态。

如果页面一直 `connecting`，且 `btmon` 里出现 `Authentication Failure`，
说明 S2 在使用旧 link key。先停掉 webapp/test，换 MAC 后重新配对：

```bash
./scripts/switchctl.sh stop
./scripts/switchctl.sh newmac
./scripts/switchctl.sh webapp
```

`enable` 默认每次也会换新 MAC，所以 Pi 重启后通常不会遇到这个问题。

#### 5. 日志

```bash
./scripts/switchctl.sh logs                 # 最新 .out
./scripts/switchctl.sh logs test_link2.out  # 指定文件
```

#### 6. 运行宏

```bash
./scripts/switchctl.sh macro macros/example.txt
```

`macros/example.txt` 使用 nuxbt 的宏语法：

```
B 0.1s
0.5s
A 0.1s
```

也可以直接传宏字符串：

```bash
./scripts/switchctl.sh macro "B 0.1s
0.5s"
```

#### 7. Web 控制页面

```bash
./scripts/switchctl.sh webapp
```

默认监听 `0.0.0.0:8000`，启动后从浏览器访问：

- 本机：`http://127.0.0.1:8000`
- 局域网：`http://<PI_IP>:8000`

只允许本机访问（推荐，配合 SSH 隧道）：

```bash
./scripts/switchctl.sh webapp -i 127.0.0.1 -p 8000
```

然后在你的电脑上建隧道：

```bash
ssh -L 8000:127.0.0.1:8000 <USER>@<PI_IP>
```

再打开 `http://127.0.0.1:8000`。

注意：

- **webapp 和 `test` 不能同时运行**，一个适配器只能创建一个控制器；先 `stop`。
- 页面**没有鉴权**，不要暴露到公网。
- 浏览器 Gamepad API 需要 HTTPS 或 localhost；局域网 HTTP 下用键盘/宏即可。
- 页面里的 “Create Pro Controller” 和 `test` 一样，需要 Switch 2 在
  Change Grip/Order 才能首次配对。

#### 8. 抓蓝牙日志

```bash
./scripts/switchctl.sh btmon 20
```

抓 20 秒 HCI 日志到 `logs/btmon-*.log`，并打印 Connect/Disconnect/Link Key
等关键事件。

### 连接流程总结

```
Pi 重启
  └─ ./scripts/switchctl.sh enable
       └─ Switch 2: Controllers -> Change Grip/Order
            └─ ./scripts/switchctl.sh test
                 └─ 连上后按一次 A 退出菜单
                      └─ 保持控制器进程运行（宏/脚本）
```

### 已知限制（实测）

1. **S2 会保存 link key，但 Pi 不保存**：连接期间
   `Paired: yes, Bonded: no`。同一个 MAC 断开后再连会
   `Authentication Failure`；用 `./scripts/switchctl.sh newmac` 换 MAC 后
   重新配对即可。`nuxbt macro -r` 重连基本不可用。
2. **不要运行 `nuxbt demo`**：它是 Switch 1 的盲导航宏，在 S2 上会误触
   Sleep。
3. **一个适配器只能模拟一个手柄**；Pi 5 板载蓝牙与 WiFi 共用天线，建议用
   有线网络。
4. **连接保持期间是稳定的**：实测 120 秒 / 53 次输入无掉线；长时稳定性
   待测。
5. `nuxbt` 的 override 在 `/run`，重启后必须重新 `enable`。
6. **休眠/唤醒**：S2 休眠时控制器不再崩溃，会进入
   `connecting`/`reconnecting`，唤醒后自动恢复；如果一直停在 `connecting`，
   执行 `newmac` + `webapp` 重新配对。保持控制器进程运行或关闭主机自动休眠
   也有帮助。
7. **残留进程会占住适配器**：异常退出后先
   `./scripts/switchctl.sh reset`。
8. **不要在用 webapp 时重启蓝牙**：会让 webapp 的 D-Bus 连接失效，表现为
   能创建控制器但一直 `connecting`。顺序永远是：先 `reset`/`newmac`，再
   `webapp`。

### 本地 nuxbt 仓库（git submodule）

上游 nuxbt 已作为 submodule 放在 `nuxbt/`：

- fork：https://github.com/lilong7676/nuxbt
- 工作分支：`switch2-local`
- 基线：`v3.3.7`
- 本地 commit：`9d5e08f`（`Local Switch 2 compatibility fixes`）
- Switch 2 PR 分支：`switch2-fixes`
- 上游 PR：https://github.com/hannahbee91/nuxbt/pull/78
- 修改说明：`nuxbt/LOCAL_CHANGES.md`

包含的改动：

- CLI `-a/--address` 改为字符串
- 控制器 discoverable 不再 180 秒超时
- webapp 对未绑定设备不再自动 reconnect（Switch 2 场景）
- webapp Socket.IO 只用 polling
- Switch 休眠导致 `ConnectionResetError` 时进入重连流程，而不是崩溃控制器
- fallback connect 后记录 Switch 的 peer 地址，避免后续重连指向 Pi 自己
- 强制提交构建好的 `web/static/dist`（Pi 上没有 Node）

当前 venv **已经通过 editable 安装使用 submodule**：

```
nuxbt.__file__ = <项目路径>/nuxbt/nuxbt/__init__.py
Editable project location: <项目路径>/nuxbt
```

以后直接修改 `nuxbt/` 下的源码，重启 webapp 就会生效；也可以直接在 clone 里
`git diff` / commit / 提 PR。需要重新安装时：

```bash
./scripts/switchctl.sh stop
./scripts/switchctl.sh install
./scripts/switchctl.sh webapp
```

`./scripts/apply_patches.sh` 现在默认作用于 `nuxbt/nuxbt`（submodule），
不再默认改 site-packages。`patches/` 目录保留作为独立备份。
