# switch2RemoteCtl

用 Raspberry Pi 5 + [nuxbt](https://github.com/hannahbee91/nuxbt) 模拟 Switch Pro Controller，
控制 Nintendo Switch 2 做重复性操作自动化。

> 当前结论：**可行，但每次新会话需要在 Switch 2 上重新配对**（S2 不保存 link key）。
> 详细实测记录见 [`docs/RESULTS.md`](docs/RESULTS.md)，计划见 [`docs/PLAN.md`](docs/PLAN.md)。

## 目录结构

```
switch2RemoteCtl/
├── docs/               # 计划与实测记录
├── scripts/
│   ├── switchctl.sh    # 统一调试入口（最常用）
│   ├── test_link.py    # 连接/输入稳定性测试
│   └── apply_patches.sh# 重装 nuxbt 后恢复本地 patch
├── macros/             # 宏文件
├── patches/            # 本地 patch 与原始文件备份
├── logs/               # 运行日志、last_mac
└── .venv/              # Python 虚拟环境
```

## 最常用命令

所有命令都在项目根目录执行：

```bash
cd ~/switch2RemoteCtl
```

### 1. Pi 重启后：启用 nuxbt 的 BlueZ override

```bash
./scripts/switchctl.sh enable
```

会要求 sudo 密码。它做四件事：写 `/run/systemd/system/bluetooth.service.d/nuxbt.conf`、
给系统 Python 加 raw socket capability、**设置一个新的随机 Nintendo OUI 蓝牙 MAC**、
重启 bluetooth 并拉起 `hci0`。
**Pi 每次重启后都要执行一次**（`/run` 是 tmpfs）。

> 为什么每次换 MAC：Switch 2 会保存手柄的 link key，但 Pi 侧不保存（`Bonded: no`）。
> 如果继续用旧 MAC，S2 会拿旧密钥认证，结果就是 `Authentication Failure`，表现为一直
> `connecting`。换新 MAC 让 S2 当成新手柄重新配对。

### 2. 连接测试（最常用）

1. Switch 2 进入 **Controllers → Change Grip/Order**
2. Pi 上执行：

```bash
./scripts/switchctl.sh test
```

脚本会等待 Switch 连接；连上后每 2 秒按一次右方向键，默认持续 120 秒。
看到光标开始移动后，在 Switch 上**只按一次 A** 退出菜单，然后在主菜单停留观察。

> 同一时间只允许一个 `test` 实例：脚本用 `logs/test.lock` 加锁。
> 如果提示“已有测试在运行”，说明还有一个实例没退出，先执行
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

### 3. 查看状态

```bash
./scripts/switchctl.sh status
```

输出包括 nuxbt 版本、plugin 状态、`hci0`、BlueZ 适配器状态、已记录的 Switch、
上次连接的 MAC 与配对状态、正在运行的进程、最新日志。

### 4. 停止正在运行的测试

```bash
./scripts/switchctl.sh stop
```

### 4.1 连不上时先 reset

如果 Switch 搜不到手柄，或之前异常退出/休眠后连不上，先执行：

```bash
./scripts/switchctl.sh reset
```

它会用 SIGINT → SIGTERM → SIGKILL 清理残留的 `test_link.py` / `nuxbt` 进程，
重启 bluetooth 并重新拉起 `hci0`，然后打印状态。

> 2026-10-24 实测：一个 9 小时前的孤儿 `test_link.py` 进程占着 raw HCI socket，
> 导致 Switch 2 重启后也搜不到手柄。`reset` 后恢复。

### 5. 运行宏

```bash
./scripts/switchctl.sh macro macros/example.txt
```

`macros/example.txt` 的格式与 nuxbt 宏一致，例如：

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

### 5.1 Web 控制页面

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
- 页面里的 “Create Pro Controller” 和 `test` 一样，需要 Switch 2 在 Change Grip/Order 才能首次配对。

### 6. 抓蓝牙日志

```bash
./scripts/switchctl.sh btmon 20
```

抓 20 秒 HCI 日志到 `logs/btmon-*.log`，并打印 Connect/Disconnect/Link Key 等关键事件。

### 4.2 一直 connecting / btmon 里出现 Authentication Failure

Switch 2 会保存手柄的 link key，而 Pi 侧不保存（`Bonded: no`）。再次用同一个 MAC 连接时，
S2 会拿旧密钥认证，结果就是反复 `Authentication Failure`，页面一直 `connecting`。

解决办法：换一个新的蓝牙 MAC，让 S2 把它当新手柄重新配对。

```bash
./scripts/switchctl.sh stop      # 必须先停掉 webapp/test
./scripts/switchctl.sh newmac    # 生成随机 Nintendo OUI MAC
./scripts/switchctl.sh webapp    # 重新启动，然后重新配对
```

`enable` 默认每次也会换新 MAC，所以 Pi 重启后通常不会遇到这个问题。

### 4.3 跟踪日志

```bash
./scripts/switchctl.sh logs            # 最新 .out
./scripts/switchctl.sh logs test_link2.out
```

## 连接流程总结

```
Pi 重启
  └─ ./scripts/switchctl.sh enable
       └─ Switch 2: Controllers -> Change Grip/Order
            └─ ./scripts/switchctl.sh test
                 └─ 连上后按一次 A 退出菜单
                      └─ 保持控制器进程运行（宏/脚本）
```

## 已知限制（实测）

1. **S2 会保存 link key，但 Pi 不保存**：连接期间 `Paired: yes, Bonded: no`。
   同一个 MAC 断开后再连会 `Authentication Failure`；用 `./scripts/switchctl.sh newmac`
   换 MAC 后重新配对即可。`nuxbt macro -r` 重连基本不可用。
2. **不要运行 `nuxbt demo`**：它是 Switch 1 的盲导航宏，在 S2 上会误触 Sleep。
3. **一个适配器只能模拟一个手柄**；Pi 5 板载蓝牙与 WiFi 共用天线，建议用有线网络。
4. **连接保持期间是稳定的**：实测 120 秒 / 53 次输入无掉线；长时稳定性待测。
5. `nuxbt` 的 override 在 `/run`，重启后必须重新 `enable`。
6. **休眠会断开**：Switch 2 休眠/唤醒后，控制器进程可能已崩溃或连接未恢复。
   保持控制器进程运行、关闭主机自动休眠，或唤醒后执行 `newmac` + `webapp` 重新配对。
7. **残留进程会占住适配器**：异常退出后先 `./scripts/switchctl.sh reset`。
8. **不要在用 webapp 时重启蓝牙**：会让 webapp 的 D-Bus 连接失效，表现为能创建控制器
   但一直 connecting。顺序永远是：先 `reset`/`newmac`，再 `webapp`。

## 本地 nuxbt 仓库

上游仓库已 clone 到 `nuxbt/`：

- 分支：`switch2-local`
- 基线：`v3.3.7`
- 本地 commit：`a67ca9e`（`Local Switch 2 compatibility fixes`）
- 修改说明：`nuxbt/LOCAL_CHANGES.md`

包含的改动：

- CLI `-a/--address` 改为字符串
- 控制器 discoverable 不再 180 秒超时
- webapp 不再自动 reconnect（S2 无 link key）
- webapp Socket.IO 只用 polling
- 模板加 `?v=polling` 防缓存
- 强制提交构建好的 `web/static/dist`（Pi 上没有 Node）

当前 venv **已经通过 editable 安装使用 clone**：

```
nuxbt.__file__ = <项目路径>/nuxbt/nuxbt/__init__.py
Editable project location: <项目路径>/nuxbt
```

以后直接修改 `nuxbt/` 下的源码就会生效（重启 webapp 后），也可以直接在 clone 里
`git diff` / commit / 提 PR。

如果需要重新安装：

```bash
./scripts/switchctl.sh stop
./scripts/switchctl.sh install     # apply patches + pip install -e ./nuxbt
./scripts/switchctl.sh webapp
```

`./scripts/apply_patches.sh` 现在默认作用于 `nuxbt/nuxbt`（clone），
不再默认改 site-packages。`patches/` 目录保留作为独立备份。
