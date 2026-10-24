# 实测记录（RESULTS）

> 与 `PLAN.md` 配套，按时间记录环境、操作、输出与结论。

---

## 2026-10-23

### 1. 环境基线

| 项 | 实测值 |
|---|---|
| 主机 | Raspberry Pi 5 Model B Rev 1.1 |
| OS / Kernel | Debian 13 (trixie) / 6.18.50+rpt-rpi-2712 aarch64 |
| 内存 | 15 GiB（可用 14 GiB） |
| 磁盘 | KIOXIA-EXCERIA SSD 465.8G（NVMe，根分区 ext4） |
| 网络 | **SSH 走 wlan0**（`<PI_IP>`），eth0 DOWN |
| 蓝牙 | hci0，UART 板载 BCM4345/6（BT 5.0），与 WiFi 共用天线 |
| 温度 / 供电 | 56.5°C；`vcgencmd get_throttled` = `0x0`（仅代表本次启动） |
| WiFi 省电 | `iw dev wlan0 get power_save` → **on** |
| journal | 非持久化（`no persistent journal was found`），重启前的日志不可查 |

### 2. 安装过程与“卡死”事故

时间线（CST）：

| 时间 | 事件 |
|---|---|
| 21:47 | 创建 venv（`--system-site-packages`），`pip install nuxbt`；拉取 PyQt6/Qt6 等约 5000 个文件 |
| ~21:51 | 用户发现终端无响应，强制重启 Pi（本次启动 ID 首条日志 21:51:01） |
| 21:52 | 检查 venv：**1999 / 5494 个文件为 0 字节** → 安装写入被断电截断 |
| 21:52–21:55 | 删除 venv，用 `--no-cache-dir` 重装，成功 |

结论与假设：

- **能确认的**：强制重启发生在 pip 大量写文件期间，导致 venv 文件被截断；重装后已恢复正常。
- **不能确认的**：是“整机真卡死”还是“SSH/WiFi 链路卡死”。由于 journal 未持久化、且没有 HDMI 侧观察记录，无法定因。
- **优先怀疑**：
  1. SSH 走 WiFi，且 `power_save=on`；WiFi 固件/链路短暂卡住时，终端表现为完全无响应，但系统可能仍在运行；
  2. 供电瞬时不足（非官方 27W 电源时，重 IO + CPU 峰值可能触发）；
  3. NVMe 在高 IO 下的瞬时挂起（本次 dmesg 未见 NVMe 错误）；
  4. 过热降频一般只会变慢，不至于 SSH 完全无响应（当时 56.5°C，暂排除）。
- **后续动作**：启用持久化 journal；尽量改用有线网络；重负载安装放 tmux/nohup 并 `nice ionice`；复现时记录 HDMI 画面与 `vcgencmd get_throttled`。

### 3. 安装结果（重装后）

```
$ .venv/bin/nuxbt --version
nuxbt, version 3.3.7

$ .venv/bin/nuxbt check
NUXBT Plugin Disabled

$ .venv/bin/nuxbt addresses
No Switches have previously connected to this device.
```

- `check` 的语义：`/run/systemd/system/bluetooth.service.d/nuxbt.conf` 不存在 → NUXBT 的 BlueZ override 未启用。
- 该 override 位于 `/run`（tmpfs），**每次重启后都需要重新执行 `nuxbt toggle`**。
- `toggle` 会做三件事（需 sudo）：
  1. 写 override，让 bluetoothd 以 `--compat --noplugin=*` 启动（禁用**所有** BlueZ 插件，包括 input）；
  2. 对 venv 解释器的真实路径（`/usr/bin/python3.13`）执行 `setcap cap_net_raw,cap_net_admin,cap_net_bind_service+eip`；
  3. `systemctl daemon-reload && systemctl restart bluetooth`。
- 风险提示：setcap 作用在**系统 Python** 上（venv 的 python 是符号链接），意味着本机任何 Python 脚本都获得 raw socket 能力；如不接受，可改为用 root 运行 nuxbt（仅宏场景可行）。

### 4. P1 执行结果（2026-10-23 21:58）

使用 root 授权执行了 `nuxbt toggle`：

```
$ .venv/bin/nuxbt check
NUXBT Plugin Enabled

$ getcap /usr/bin/python3.13
/usr/bin/python3.13 cap_net_bind_service,cap_net_admin,cap_net_raw=eip

$ ps -eo args | grep bluetoothd
/usr/libexec/bluetooth/bluetoothd --compat --noplugin=*
```

- 重启 bluetooth 后 `hci0` 为 DOWN，已执行 `sudo hciconfig hci0 up`，恢复 UP RUNNING / Powered: yes。
- 21:58:53 启动 `nuxbt -d demo`（后台，600s 超时），控制器创建成功：
  - 适配器 `/org/bluez/hci0`
  - Alias: `Pro Controller`
  - Class: `0x02508`
  - 状态：等待 Switch 2 连接
- 日志：`logs/demo.out`

### 5. P2 首次配对（部分成功）

Switch 2 于 **21:59:45** 发起配对，MAC `78:81:8C:16:59:B7`；控制器初始化握手完整执行
（`0x21` 子命令交换、SPI 读取、摇杆校准数据等），**用户确认手柄在动**。

但 22:00:09 宏接近结束时，Switch 2 主动断开：

```
ConnectionResetError: [Errno 104] Connection reset by peer
  File ".../nuxbt/controller/server.py", line 194, in mainloop
    reply = itr.recv(50)
```

关键证据：

- `/var/lib/bluetooth/88:A2:9E:85:B6:EA/78:81:8C:16:59:B7/` **不存在** → S2 没有保存配对信息（与 issue #67 的 No Bonding 一致）。
- `nuxbt addresses` → `No Switches have previously connected to this device.`
- `bluetoothctl devices` 为空。
- 宏总时长约 20.7s，实际从连接至断开约 23.5s，说明宏**接近完成**，断开发生在退出 Change Grip/Order 的收尾阶段。

结论：**首次配对与控制可行**；断开符合社区已知的 S2 行为，不一定是致命问题，但意味着
“每次运行都要重新进 Change Grip/Order / 重新配对”的风险很高，需要 P4 重点验证。

### 7. 休眠事故与重试（22:03–22:06）

- demo 宏按 Switch 1 菜单布局盲导航，在 Switch 2 上最终选中了 **Sleep**，主机休眠。
- 用户手动唤醒后，手柄断连；Switch 2 的 Change Grip/Order 页面搜不到 “Pro Controller”。
- Pi 侧检查：
  - `bluetoothctl info 78:81:8C:16:59:B7` → `Paired: no, Bonded: no, Connected: no`
  - `/var/lib/bluetooth/.../78:81:8C:16:59:B7/info` 不存在（未保存 link key）
  - `nuxbt addresses` 能列出该 MAC（来自 BlueZ 的 device cache，不代表已配对）
- 处理：重启 bluetooth 服务 → `hciconfig hci0 up` → 重新创建控制器（`test_link.py --timeout 600`），等待 Switch 2 重启后重新配对。
- **教训**：不要在 Switch 2 上运行 `nuxbt demo`；盲导航宏可能触发休眠/退出/购买等危险操作。后续宏应只针对明确界面编写。

### 8. P2/P4 稳定性测试：120 秒通过（22:07–22:10）

Switch 2 彻底重启后重新配对成功：

```
connected to 78:81:8C:16:59:B7
sent DPAD_RIGHT #1 ... #53 (120.2s)
done, still connected: connected
```

- 测试期间用户按一次 A 退出 Change Grip/Order，**连接未断开**。
- 全程 120 秒、53 次输入，无掉线。
- 连接期间：`Paired: yes, Bonded: no, Connected: yes`。
- 测试结束后 `remove_controller` 释放适配器，`bluetoothctl` 状态变为 `Paired: no, Bonded: no`。

**结论**：一旦配对成功，只要保持控制器进程运行，Switch 2 可以稳定接受输入并维持连接至少 2 分钟；
退出配对菜单不会必然掉线（与 #67 的“退出即掉”不完全一致，可能与 S2 固件/时序有关）。

### 9. 重连测试：未通过（22:10）

- `nuxbt macro -a 78:81:8C:16:59:B7 ...` → **CLI bug**：`--address` 被声明为 `is_flag=True`，任何 MAC 都会被当成布尔值解析失败。
- 改用 `nuxbt macro -r ...`（按 alias 查找）→ 40 秒内未连接成功，日志中只看到：
  - `RequestPinCode (/org/bluez/hci0/dev_78_81_8C_16_59_B7)`
  - 之后无 `connected`，超时退出（exit 124）。
- 原因判断：S2 未保存 link key（`Bonded: no`），无法用已保存密钥重连；`-r` 回退到等待 Switch 主动连接，但当时 Switch 不在 Change Grip/Order，未完成配对。
- **实际影响**：每次重新开始自动化，可能都需要 Switch 2 进一次 Change Grip/Order 重新配对；不能依赖“开机自动重连”。

### 10. 工具封装与本地 patch（22:35–22:40）

- **修复 CLI bug**：`nuxbt` 的 `macro` / `tui` / `remote-tui` 中
  `-a/--address` 被声明为布尔 flag（`default=False`），无法传 MAC。
  已改为 `default=None, type=str`，`nuxbt macro --help` 现在显示 `-a, --address TEXT`。
  - 备份：`patches/cli.py.orig`
  - patch：`patches/nuxbt-3.3.7-address-option.patch`
  - 重装后恢复：`./scripts/apply_patches.sh`
- **新增统一调试入口** `scripts/switchctl.sh`：
  - `enable` / `disable`：写入或回滚 BlueZ override，拉起 `hci0`
  - `status`：版本、plugin、适配器、配对、进程、日志
  - `test` / `pair`：连接测试（默认 120s，每 2s 按一次 DPAD_RIGHT）
  - `reconnect [MAC]`：测试重连（S2 预期超时，带 45s 超时保护）
  - `macro <文件|字符串> [参数]`：运行宏
  - `btmon [秒]`、`logs [文件]`、`stop`、`env`、`demo`（带风险确认）
- **新增 README** `README.md`：记录常用命令、连接流程、已知限制。
- 验证：
  - `bash -n scripts/switchctl.sh` 通过
  - `./scripts/switchctl.sh status` 正常
  - `./scripts/switchctl.sh enable` 成功（需 sudo，已在缓存凭据下测试）
  - `./scripts/switchctl.sh test --help` 参数透传正常
  - `RECONNECT_TIMEOUT=12 ./scripts/switchctl.sh reconnect 78:81:8C:16:59:B7`
    → 使用 patched `-a` 路径，12s 后超时并给出友好提示

### 11. 下一步

- 用户自行重跑连接测试：`./scripts/switchctl.sh test`
- P3：根据用户指定的游戏/操作编写真实宏
- P4：5–10 分钟连接保持测试（`./scripts/switchctl.sh test --seconds 600 --interval 5`）

### 12. 休眠后无法连接 / 残留进程（2026-10-24 08:10）

用户反馈：Switch 2 休眠再唤醒后连不上，重启 Switch 2 也无效。

Pi 侧检查发现：

- `pgrep` 有 **7 个 `test_link.py` 进程**：
  - 1 个是 2026-10-23 22:38 启动的孤儿进程（PPID 1，已运行 9h29m），SIGINT 无法杀死；
  - 6 个是用户刚启动的测试进程（父子关系正常）。
- `bluetoothctl devices` 为空，`bluetoothctl info 78:81:8C:16:59:B7` → `not available`。
- 孤儿进程占着 raw HCI socket，导致适配器无法正常创建新的控制器 / 广播。

处理：

1. `kill -9 188495` 杀掉孤儿进程；
2. 清理其余测试进程；
3. 重启 bluetooth + `hciconfig hci0 up`；
4. 给 `switchctl.sh` 增加 `reset` 命令，并将 `stop` 改为 SIGINT → SIGTERM → SIGKILL 逐级清理；
5. `test` 子命令增加 EXIT trap，退出时清理 `test_link.py` 残留。

结论：用户看到的“Switch 2 休眠后连不上”**主要不是 S2 的问题，而是 Pi 侧控制器进程异常残留**。
后续需要验证：控制器进程存活时，S2 休眠→唤醒是否能自行恢复；若不能，则唤醒后必须
`reset` + `test` 重新配对，或在自动化期间关闭主机自动休眠。

### 13. 重复运行 test 导致“timeout waiting for connection”（2026-10-24 08:15）

用户执行 `./scripts/switchctl.sh test`，Switch 显示已连接，但脚本几分钟后报
`timeout waiting for connection` 并打印 `SystemExit: 0` traceback。

原因：

- 08:12 我们启动的后台 `test_link3` 仍在运行，并已连接 Switch；
- 用户又启动了第二个 `test`，它拿不到适配器/连接，等待 180 秒后超时；
- 第二个实例退出时，旧的 EXIT trap 执行 `pkill -f test_link.py`，把**所有**实例
  （包括已连接的 `test_link3`）一起杀掉，于是出现 traceback。

修复：

- `switchctl.sh test` 增加 `flock` 锁（`logs/test.lock`），第二个实例直接拒绝启动；
- 清理逻辑改为只针对自己的子进程，不再全局 `pkill`；
- `stop` 改为 SIGINT → SIGKILL（避免 nuxbt 子进程的 `SystemExit` traceback）；
- `test_link.py` 覆盖 SIGTERM handler，并在 `finally` 中显式调用 `_on_exit()`；
- `reset` 同时删除锁文件。

验证：

- 第二个 `test` 立即输出“已有测试在运行”，exit 1；
- `stop` 后无 traceback、无残留进程；
- `reset` 后 `hci0` UP、无 `test_link`/`nuxbt` 进程。

### 14. 一直 connecting 的根因：Authentication Failure（08:49–08:56）

用 `btmon` 抓包后确认，Switch 2 一直在反复发起连接，但每次认证失败：

```
> HCI Event: Connect Request            Address: 78:81:8C:16:59:B7
< HCI Command: Accept Connection Request
> HCI Event: Connect Complete
> HCI Event: Remote Name Req Complete   Status: Authentication Failure (0x05)
> HCI Event: Disconnect Complete        Reason: Authentication Failure (0x05)
```

原因：Switch 2 保存了旧 MAC `88:A2:9E:85:B6:EA` 的 link key，而 Pi 侧没有保存
（`Bonded: no`），所以 S2 用旧密钥认证必然失败。重启 Switch 2 不会清除该密钥。

解决：把 Pi 的蓝牙 MAC 改成新的 Nintendo OUI 地址：

```
BD Address: 7C:BB:8A:53:32:11
```

之后 Switch 2 把它当成全新手柄，配对成功。最终状态：

```
STATE 1 connected last= 78:81:8C:16:59:B7 errors= False
Paired: yes, Bonded: no, Connected: yes
```

过程中还观察到一次 `L2CAP: Connection Response - PSM not supported (0x0002)` 对 PSM 17/19
的拒绝，说明当时控制器进程还没完成监听（webapp 内部有多个控制器/锁竞争）。
清理进程 + 重启 Switch 后恢复正常。

固化到脚本：

- `switchctl.sh enable` 默认生成随机 Nintendo OUI MAC；`newmac [MAC]` 可手动换 MAC。
- `apply_patches.sh` 新增 patch：`controller.py` 的 `set_discoverable_timeout(180)` → `0`，
  避免等待超过 3 分钟后停止广播。
- `app.py` 创建控制器时不再自动 `reconnect_address`（S2 无 link key）。
- 前端 Socket.IO 改为 polling-only；模板加 `?v=polling` 防止浏览器缓存旧 JS。

**重要操作顺序**：先 `enable`/`newmac`（会重启蓝牙），再启动 webapp。不要在 webapp
运行时重启蓝牙，否则它的 D-Bus 连接会失效，表现为能创建控制器但一直 connecting。

### 15. 切换到本地 clone + editable 安装（09:05）

- 上游仓库 clone 到 `nuxbt/`，分支 `switch2-local`，基线 `v3.3.7`。
- 本地 commit：`a67ca9e`（`Local Switch 2 compatibility fixes`）。
- 构建好的 `web/static/dist` 与 `templates/index.html` 强制提交（Pi 上没有 Node）。
- `pip install -e ./nuxbt --no-deps` 成功：
  - `nuxbt.__file__` → `switch2RemoteCtl/nuxbt/nuxbt/__init__.py`
  - `pip show nuxbt` → `Editable project location: .../nuxbt`
- `enable` 生成新 MAC `7C:BB:8A:3F:96:40`，重启蓝牙并启动 webapp。
- 页面验证：`index-Dc9-PrRS.js?v=polling`，JS 中 `transports:["polling"]`。









