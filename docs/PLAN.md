# switch2RemoteCtl 实施与验证计划

> 目标：在本机（Raspberry Pi 5）上用 [nuxbt](https://github.com/hannahbee91/nuxbt) 模拟 Switch 手柄，
> 控制 Nintendo Switch 2 完成重复性操作自动化（宏 / Python API）。
>
> 本文档是“活文档”：每完成一步就更新状态、记录命令与结果。

---

## 1. 背景与结论

### 1.1 nuxbt 是什么

- `hannahbee91/nuxbt` 是已停更项目 `Brikwerk/nxbt` 的活跃 fork。
- 原理：通过 BlueZ raw HCI，让本机蓝牙适配器伪装成 **Switch 1 Pro Controller**
  （Bluetooth Classic BR/EDR + HID，非 BLE）。
- 能力：Webapp / TUI / CLI 宏 / Python API；支持宏排队、循环、按住时长。
- 限制：一个蓝牙适配器只能模拟一个手柄；需要禁用 BlueZ `input` 插件。

### 1.2 对 Switch 2 的支持现状

README 的 Future Plans 明确列有 **“Add support for Switch 2 controller emulation”**，
即官方尚未支持。社区 issue 汇总：

| Issue | 现象 | 影响 |
|---|---|---|
| [#52](https://github.com/hannahbee91/nuxbt/issues/52) | Pi 4B 对 Switch 2 首次配对始终失败，对 Switch 1 正常 | 阻断级 |
| [#67](https://github.com/hannahbee91/nuxbt/issues/67) | 首连成功，30 秒后 S2 删除配对；重连必须重进 Change Grip/Order。根因：S2 协商 No Bonding，store hint = No | 高（挂机场景致命） |
| [#40](https://github.com/hannahbee91/nuxbt/issues/40) | 连接与宏可用，但 10–30 轮后随机掉线；Intel 9260 / Edimax 均复现 | 中高（长时任务） |
| 作者回复 | 作者自己的 Switch 2 无法复现上述问题 | 说明与适配器/固件强相关 |

### 1.3 本机环境快照（2026-10-23）

| 项 | 值 |
|---|---|
| 主机 | Raspberry Pi 5 (`pi5`) |
| OS | Debian GNU/Linux 13 (trixie), aarch64 |
| Kernel | 6.18.50+rpt-rpi-2712 |
| 蓝牙 | `hci0`，UART 板载，BT 5.0，Cypress/Infineon（与 WiFi 共用天线） |
| 适配器数量 | 1 → 只能模拟 1 个手柄 |
| Python | 3.13.5（nuxbt 要求 ≥3.12） |
| 工具 | `bluetoothctl` / `hciconfig` 可用；`pipx` 未安装 |
| sudo | 需要密码（自动化步骤中需人工协助） |
| 软件包 | Debian 源无 nuxbt，官方 PPA 仅支持 Ubuntu Noble/Resolute → 需手动 venv 安装 |

### 1.4 可行性判断

**可行，但非开箱即用。** 成败取决于：
1. 板载 CYW 蓝牙在 HID 角色下能否完成 S2 首次配对；
2. S2 是否保存配对（重连）；
3. 长时运行是否随机掉线。

因此本计划以“**逐级验证、每级有明确通过标准**”的方式推进，任一环节失败都有替代方案。

---

## 2. 验证路径（分阶段）

### P0 — 环境准备（✅ 已完成 2026-10-23）

- [x] 创建项目目录结构
- [x] 创建 Python venv（系统 Python 3.13，`--system-site-packages` 复用 python3-dbus / python3-gi）
- [x] `pip install nuxbt` → **3.3.7**
- [x] 系统依赖：`python3-dbus` / `python3-gi` 已预装，无需编译 dbus-python/PyGObject
- [x] `nuxbt --help` / `nuxbt check` 可运行

**实测结果**：`nuxbt check` → `NUXBT Plugin Disabled`（即 override 未启用）。
详见 `RESULTS.md` 的“安装过程与卡死事故”。

### P1 — 静态检查与蓝牙权限（✅ 已完成 2026-10-23）

- [x] 确认 BlueZ `input` 插件状态（override 未启用，插件仍在运行）
- [x] `nuxbt toggle` 启用 override（已用 root 授权执行）
- [x] 确认 `hci0` 未被其他服务占用（`Address already in use` 检查）
- [x] 记录原始配置，便于回滚

**实测结果**：

- `/run/systemd/system/bluetooth.service.d/nuxbt.conf` 已创建：
  `ExecStart=/usr/libexec/bluetooth/bluetoothd --compat --noplugin=*`
- `getcap /usr/bin/python3.13` → `cap_net_bind_service,cap_net_admin,cap_net_raw=eip`
- `nuxbt check` → `NUXBT Plugin Enabled`
- 重启 bluetooth 后 `hci0` 变为 DOWN（`--noplugin=*` 不再自动上电），已执行 `sudo hciconfig hci0 up` 恢复为 UP RUNNING / Powered: yes。

**注意**：override 写在 `/run`（tmpfs），每次重启后需重新 `nuxbt toggle`；且 `hci0` 可能需要手动 `hciconfig up`。

**通过标准**：`nuxbt check` 显示 input 插件已禁用；`hciconfig` 仍正常。

### P2 — 首次配对（Switch 2）（✅ 通过，2026-10-23）

前置：Switch 2 开机，进入 **Controllers → Change Grip/Order** 界面并停留。

- [x] 运行 `nuxbt demo`（会创建 Pro Controller）
- [x] 观察 S2 是否识别为 “Pro Controller”
- [x] 记录 `btmon` / `bluetoothctl` 日志
- [x] 若失败：换 USB 蓝牙 dongle 重试（备选方案 A）

**实测结果**：demo 成功配对并执行宏（用户确认手柄在动），但宏结尾 Switch 2 主动断开
（`ConnectionResetError`），且 S2 未保存配对信息。**不要在 S2 上使用 demo**：其盲导航
在 S2 菜单布局下会误触 Sleep。详见 `RESULTS.md` 第 5、7 节。

补充：2026-10-23 22:07 重启 S2 后重新配对，`test_link.py` 连续 120 秒、53 次输入无掉线，
且退出 Change Grip/Order 后连接保持。**P2 判定通过。**

### P3 — 宏与 API 控制（进行中）

- [ ] CLI 宏：`nuxbt macro -c "B 0.1s\n0.1s" -r`（需先配对成功）
- [x] Python API 最小示例（`create_controller` / `press_buttons` / `tilt_stick`，见 `test_link.py`）
- [x] 编写一个可复用的自动化脚本骨架（`scripts/test_link.py` + `scripts/switchctl.sh`）
- [ ] 验证宏排队 / 非阻塞模式
- [ ] 按用户指定的游戏/操作编写真实宏
- [x] 修复 CLI `-a/--address` 布尔 flag bug（本地 patch + `scripts/apply_patches.sh`）

**通过标准**：脚本能稳定完成一段 1–2 分钟的重复操作序列。

### P4 — 稳定性与重连（部分完成）

- [ ] 连续运行 15–30 分钟宏，记录掉线时间点
- [x] 测试断开后重连（`reconnect_address`）→ **未通过**：S2 无 link key，`-r` 超时
- [x] 测试 S2 退出 Change Grip/Order 后是否掉线 → **通过**：120s 测试中未掉线
- [ ] 若随机掉线：关闭 WiFi / 改用有线、调整蓝牙电源管理、缩短宏间隙并发保活输入
- [ ] 若重连失败：尝试 issue #67 的 LinkKey 固化 workaround（固定 MAC 前缀 `7C:BB:8A` + 手写 `/var/lib/bluetooth/.../info`）

**通过标准**：30 分钟无掉线，或掉线后可自动恢复。

### P5 — 决策与后续

- [ ] 汇总实测结果，给出“可用 / 有条件可用 / 不可用”结论
- [ ] 若可用：完善自动化框架（宏库、CLI、日志、失败重试）
- [ ] 若不可用：评估备选方案
  - 备选 A：独立 USB 蓝牙 dongle（避开板载 WiFi/BT 共存）
  - 备选 B：采集卡 + 图像识别闭环（解决“开环宏不知道游戏状态”）
  - 备选 C：硬件手柄模拟器 / 其他社区方案（重新调研 2026 年 S2 支持情况）

---

## 3. 风险与约束

1. **官方不支持 S2**：任何一步失败都可能是上游协议问题，不保证能修。
2. **单适配器单手柄**：需要多手柄得加 dongle。
3. **禁用 input 插件**会导致板载蓝牙键鼠失效 → 用 SSH / USB 键鼠。
4. **板载 WiFi/BT 共存**：2.4GHz 干扰是掉线高发原因。
5. **开环控制**：宏只知道时间，不知道画面状态；闭环需采集卡。
6. **账号风险**：联网/竞技游戏使用自动化可能违反任天堂服务条款。
7. **回滚**：所有系统级修改（input 插件、bluetooth 配置）都要先备份并记录恢复命令。

---

## 4. 目录结构（约定）

```
switch2RemoteCtl/
├── docs/
│   ├── PLAN.md          # 本文件
│   └── RESULTS.md       # 实测记录（随进度更新）
├── scripts/             # 自动化脚本
├── macros/              # 宏文件
├── logs/                # 运行与调试日志（btmon 等）
└── .venv/               # Python 虚拟环境（不入库）
```

---

## 5. 需要人工协助的事项

| 阶段 | 事项 | 原因 |
|---|---|---|
| P0/P1 | 输入 sudo 密码（安装依赖、`nuxbt toggle`） | 当前会话 sudo 需密码 |
| P1 | 尽量改用**有线网络**（eth0 当前 DOWN），或关闭 WiFi 省电 | SSH 走 wlan0，而蓝牙与 WiFi 共用板载天线；实测已发生过一次“终端无响应” |
| P2 | 在 Switch 2 上打开 Change Grip/Order 并观察屏幕 | 物理操作 |
| P2 | 反馈 S2 是否识别到手柄、是否弹出配对提示 | 物理观察 |
| P4 | 长时测试期间观察游戏画面/掉线现象 | 物理观察 |

### 已发生事故

- 2026-10-23 21:47–21:51：首次 `pip install` 期间终端无响应，用户强制重启；重启后 venv 内 1999 个文件为 0 字节。
- 已通过重装修复。**原因未定**（journal 未持久化）；详见 `RESULTS.md`。
- 复现时请记录：HDMI 是否有画面、`vcgencmd get_throttled`、`dmesg` 尾部、SSH 是仅网络断还是整机无响应。


---

## 6. 执行记录

| 时间 | 阶段 | 操作 | 结果 |
|---|---|---|---|
| 2026-10-23 21:47 | P0 | 创建目录、venv、安装 nuxbt | 安装期间系统卡死，21:51 强制重启，venv 文件被截断 |
| 2026-10-23 21:52 | P0 | 删除 venv 重装（`--no-cache-dir`） | ✅ nuxbt 3.3.7 可用；`nuxbt check` 正常 |
| 2026-10-23 21:56 | P1 | 等待 sudo 权限以执行 `nuxbt toggle` | ⏸ 阻塞，需人工协助 |
| 2026-10-23 21:58 | P1 | 执行 `nuxbt toggle` + `hciconfig up` | ✅ Plugin Enabled，`hci0` UP |
| 2026-10-23 21:59 | P2 | `nuxbt demo` 首次配对 | ✅ 配对成功、手柄在动；结尾 S2 断开，且误触 Sleep |
| 2026-10-23 22:07 | P2/P4 | S2 重启后重新配对，`test_link.py` 120s | ✅ 53 次输入无掉线，退出菜单后连接保持 |
| 2026-10-23 22:10 | P4 | `-a` / `-r` 重连测试 | ❌ 无 link key，重连超时；`-a` 发现 CLI bug |
| 2026-10-23 22:35 | P3 | patch `--address` bug + 封装 `switchctl.sh` | ✅ 脚本可用，README 已补 |
| 2026-10-24 08:56 | P2/P4 | 换 MAC + 重启 S2 后重新配对 | ✅ webapp `connected`，S2 正常连接 |
| 2026-10-24 10:03 | P4 | 修复休眠 `ConnectionResetError` 崩溃 | ✅ PR #78 更新，待复测休眠/唤醒 |



