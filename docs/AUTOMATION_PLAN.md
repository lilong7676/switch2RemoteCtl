# 自动化初步方案：牧场物语 风之繁华集市

> 状态：草案 v0.1  
> 目标：Mac 端负责采集卡画面与视觉识别，Pi 5 端继续用 nuxbt 模拟 Switch 2 Pro Controller。  
> 本文档同时作为 Mac 端环境搭建和 Socket.IO 控制协议的说明。  
> 仓库：<https://github.com/lilong7676/switch2RemoteCtl>  
> 相关文档：[README](../README.md)、[PLAN](PLAN.md)、[RESULTS](RESULTS.md)

---

## 0. TL;DR：Mac 先做这 5 步

1. 克隆仓库并创建 Mac 端 Python 环境：

   ```bash
   git clone --recurse-submodules https://github.com/lilong7676/switch2RemoteCtl.git
   cd switch2RemoteCtl

   python3 -m venv .venv-mac
   source .venv-mac/bin/activate
   python -m pip install -U pip
   pip install opencv-python numpy python-socketio requests pyyaml
   ```

2. 把采集卡接到 Mac，Switch 2 放到底座，系统设置改成 **1080p60、HDR 关闭**。

3. 给 Terminal / iTerm / VS Code 打开 macOS 的 **相机权限**，然后用下面的命令确认采集卡设备号：

   ```bash
   brew install ffmpeg
   ffmpeg -f avfoundation -list_devices true -i ""
   ```

4. 运行采集自检，确认能到 1080p60，并保存一张 `reference.png` 用于后续标定。代码见 [5.5](#55-采集卡自检mac)。

5. 运行 Socket.IO 测试客户端，确认能读到 Pi 上 webapp 的控制器状态，并发送一个无害的 `0.1s` 宏。代码见 [4.4](#44-python-socketio-客户端完整示例)。

完成这 5 步后，再开始做第一个视觉任务：**自动浇水**。

---

## 1. 目标与范围

### 1.1 目标

在 **离线/单人** 模式下，自动完成《牧场物语：风之繁华集市》的重复劳动：

- 自动浇水
- 自动施肥
- 自动收获作物
- 自动去风车/料理点制作东西
- 自动钓鱼
- 后续组合成每日循环：起床 → 浇水/施肥 → 收获 → 风车 → 钓鱼 → 睡觉

### 1.2 非目标

- 不做在线对战、排名、联机内容。
- 不修改游戏存档、不读取游戏内存、不绕过游戏机制。
- 不把 webapp 暴露到公网。
- 不在没有备份存档的情况下跑长时间无人值守任务。

### 1.3 关键结论

这款游戏不能只靠“固定延时宏”完成全部任务：

- **浇水/施肥/收获** 需要知道角色位置、朝向、地块状态、体力、背包，适合“固定路线 + 视觉校验”。
- **风车加工/料理** 主要是菜单操作，可以先做纯宏，再用 OCR/模板校验。
- **钓鱼** 咬钩后是按键 QTE，屏幕会提示按钮顺序，必须实时看画面，纯宏不可行。

因此推荐架构是：

```text
Switch 2 ──HDMI──> 采集卡 ──USB──> Mac
                                   │
                            OpenCV / 状态机
                                   │
                              Socket.IO
                                   │
                                   ▼
                    Pi 5 (nuxbt webapp) ──蓝牙──> Switch 2
```

Mac 是“眼睛和大脑”，Pi 是“手”。采集卡不是可选项，尤其对钓鱼来说。

---

## 2. 总体架构

### 2.1 数据流

```text
┌──────────────┐   HDMI    ┌──────────────┐   USB/UVC   ┌────────────────────┐
│ Nintendo     │──────────>│ HDMI 采集卡   │────────────>│ Mac                │
│ Switch 2     │           │ (UVC)        │             │ OpenCV + 状态机     │
└──────┬───────┘           └──────────────┘             └─────────┬──────────┘
       │                                                           │
       │ Bluetooth (Pro Controller)                                │ Socket.IO
       │                                                           │ polling
       ▼                                                           ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ Raspberry Pi 5                                                              │
│   nuxbt webapp (:8000)  ──>  nuxbt controller process  ──>  Bluetooth        │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 为什么不让 Pi 同时做视觉

可以，但不推荐作为第一版：

- Pi 5 能跑 OpenCV，1080p30 的模板匹配没问题；但钓鱼 QTE 对延迟敏感，Mac 的 CPU/GPU 和调试体验更好。
- Mac 可以直接用 OpenCV 窗口看画面、框 ROI、保存模板，迭代快。
- Pi 继续只做它最擅长的事：稳定地模拟 Pro Controller。
- 如果以后想一体化，再把视觉逻辑移植到 Pi 5；Socket 协议不变。

### 2.3 延迟预算

| 环节 | 典型延迟 | 说明 |
|---|---:|---|
| 采集卡 | 40–200 ms | 低延迟卡约 40–60 ms；廉价 UVC 可能 100–200 ms |
| Mac 图像处理 | 5–30 ms | 只处理 ROI 时会更快 |
| Socket.IO polling（局域网） | 2–20 ms | 每次 emit 是一次 HTTP 请求 |
| Pi 蓝牙发送 | 10–20 ms | Pro Controller 约 120 Hz |
| 游戏响应 | 16–50 ms | 1–3 帧 |
| **合计** | **约 80–300 ms** | 回合制/菜单足够；钓鱼 QTE 需要低延迟卡 |

> 结论：浇水和菜单任务对延迟不敏感；钓鱼建议直接买低延迟 1080p60 UVC 采集卡，不要用“4K 输入、1080p30 输出”的廉价卡。

---

## 3. Pi 端当前状态

### 3.1 已具备

- Raspberry Pi 5，板载 Bluetooth（`hci0`）。
- nuxbt 3.3.7 以 **editable** 模式安装在 `~/switch2RemoteCtl/.venv`。
- nuxbt 源码是 git submodule：`nuxbt/`，分支 `switch2-local`。
- BlueZ override：`bluetoothd --compat --noplugin=*`。
- webapp：`http://<PI_IP>:8000`，Socket.IO **只支持 polling**。
- 已验证 Switch 2 配对、按键、120 秒稳定性测试。

### 3.2 启动顺序（重要）

先启动蓝牙/轮换 MAC，再启动 webapp：

```bash
cd ~/switch2RemoteCtl

# 1. 停掉旧的 webapp/test
./scripts/switchctl.sh stop

# 2. 启用 BlueZ override，并生成新的 Nintendo OUI MAC
#    需要 sudo；每次 Pi 重启后都要执行
./scripts/switchctl.sh enable

# 3. 后台启动 webapp
nohup ./scripts/switchctl.sh webapp -i 0.0.0.0 -p 8000 \
  >> logs/webapp.log 2>&1 &
```

然后：

1. 浏览器打开 `http://<PI_IP>:8000`。
2. 点一次 **Create Pro Controller**。
3. Switch 2 进入 **Controllers → Change Grip/Order**。
4. 等待新的 “Pro Controller” 出现并配对。
5. 用 `./scripts/switchctl.sh status` 或 Socket `state` 确认 `connected`。

> **不要**在 webapp 运行时执行 `enable` / `newmac` / `reset`。这些操作会重启蓝牙，webapp 的 D-Bus 连接会失效，表现为“能创建控制器但一直 connecting”。

### 3.3 配对与 MAC 轮换

Switch 2 会保存自己这边的 link key，但 Pi 上的 BlueZ 显示 `Bonded: no`，不会持久化。重复使用同一个 MAC 会让 Switch 2 用旧密钥认证，最终出现：

```text
Authentication Failure (0x05)
```

当前解决办法：

- 每次新会话/重启后执行 `./scripts/switchctl.sh enable`（会换新 MAC）。
- 或者在卡住时：

  ```bash
  ./scripts/switchctl.sh stop
  ./scripts/switchctl.sh newmac
  ./scripts/switchctl.sh webapp
  ```

- Switch 2 会把这当成一个全新的 Pro Controller，需要重新配对。

webapp 里创建控制器时，本地补丁已经**不再传 `reconnect_address`**，所以 Switch 2 总是走一次全新配对；这是预期行为。

### 3.4 控制器生命周期（关键）

webapp 的控制器是**绑定 Socket.IO 客户端**的：

- 浏览器连接后点 Create，会创建控制器，并把 `controller_index` 记录在 `USER_INFO[sid]`。
- 这个浏览器 socket 断开时，`on_disconnect` 会调用 `nuxbt.remove_controller(index)`。
- 因此：**浏览器标签页关掉，控制器就没了**。

为了自动化，有两种模式：

#### 模式 A（第一版推荐）：浏览器持有控制器，Mac 只发指令

```text
浏览器（一直开着）  ──创建/持有 controller 0
Mac 视觉客户端       ──第二个 Socket.IO 连接，只发 macro/state
```

优点：

- Mac 断开/重启不会移除控制器。
- 配对仍然由浏览器 UI 完成，最稳。
- 不会因为 Mac 脚本崩溃而立刻掉线。

要求：

- 浏览器标签页保持打开。
- Mac 关闭自动睡眠，或使用 `caffeinate`。
- Mac 客户端**不要**调用 `web_create_pro_controller`，**不要**调用 `shutdown`。

#### 模式 B（后续）：Mac/无头客户端持有控制器

Mac 连接后自己发 `web_create_pro_controller`，自己持有 controller。适合无人值守，但：

- Mac socket 断开时控制器会被移除。
- 重新创建时 Switch 2 可能因为旧 link key 认证失败，需要换 MAC + 重新配对。
- 需要把 webapp 改成“控制器常驻、不绑定 socket”后才适合长期跑。

**第一阶段请用模式 A。**

---

## 4. 通过 Socket.IO 操作 Switch

### 4.1 连接参数

| 项目 | 值 |
|---|---|
| URL | `http://<PI_IP>:8000` |
| 传输 | **仅 polling**：`transports=["polling"]` |
| WebSocket | 不可用（`uvicorn + a2wsgi` 不支持） |
| CORS | `*` |
| 鉴权 | 无；只允许可信局域网 |
| 默认 path | `/socket.io` |

> 不要配置 `transports=["websocket"]`，会连接失败。  
> 不要把 8000 端口映射到公网。

### 4.2 事件表

| 事件 | 方向 | 参数 | 说明 |
|---|---|---|---|
| `connect` | 内置 | 无 | 连接成功 |
| `state` | Client → Server | 无 | 请求当前状态 |
| `state` | Server → Client | `dict` | 控制器状态；只回给请求的客户端 |
| `web_create_pro_controller` | Client → Server | 无 | 创建 Pro Controller（模式 B） |
| `create_pro_controller` | Server → Client | `index: int` | 创建成功，返回控制器 index |
| `macro` | Client → Server | JSON 字符串 `[index, macro_text]` | 执行宏；ack 返回 `macro_id` |
| `stop_all_macros` | Client → Server | 无 | 清空所有控制器上正在跑/排队的宏 |
| `input` | Client → Server | JSON 字符串 `[index, packet]` | 直接输入一个协议周期 |
| `shutdown` | Client → Server | `index: int` | 移除控制器 |
| `error` | Server → Client | `str` | 创建控制器等操作出错 |

`state` 的典型结构：

```json
{
  "0": {
    "state": "connected",
    "last_connection": "<SWITCH_MAC>",
    "finished_macros": [],
    "errors": false,
    "type": "ControllerTypes.PRO_CONTROLLER",
    "adapter_path": "/org/bluez/hci0",
    "direct_input": { "...": "..." }
  }
}
```

`state` 字段：

- `state`：`initializing` / `connecting` / `reconnecting` / `connected` / `crashed`
- `last_connection`：Switch 的蓝牙 MAC
- `finished_macros`：已完成的 macro id 列表
- `errors`：`False` 或错误堆栈
- `direct_input`：当前直接输入包模板

**注意**：JSON 里的 key 是字符串，Python 里取 controller index 要 `int(key)`。

### 4.3 模式 A / B 的操作差异

#### 模式 A：浏览器已经创建了控制器

Mac 端只做：

```python
state = client.refresh_state()
index = client.connected_index()
client.macro("A 0.1s\n0.5s", index=index)
```

不要调用：

```python
client.create_controller()   # 会创建第二个控制器，单适配器会失败
client.shutdown(index)       # 会移除浏览器持有的控制器
```

#### 模式 B：Mac 自己创建控制器

```python
index = client.create_controller()      # 需要 Switch 在 Change Grip/Order
client.wait_connected(index, timeout=120)
client.macro("A 0.1s", index=index)
```

Mac 断开后控制器会被移除。第一版不推荐。

### 4.4 Python Socket.IO 客户端（完整示例）

保存为 `vision/switch_client.py`（或先放在临时目录）：

```python
#!/usr/bin/env python3
"""Minimal Socket.IO client for the nuxbt webapp.

Mode A: browser owns the controller; this client only sends state/macro.
Mode B: call create_controller() yourself; keep the socket alive.
"""

import copy
import json
import threading
import time

import socketio


class SwitchClient:
    def __init__(self, url: str):
        self.url = url
        self.sio = socketio.Client(
            reconnection=False,
            logger=False,
            engineio_logger=False,
        )
        self.state = {}
        self._state_event = threading.Event()
        self._created_event = threading.Event()
        self._created_index = None
        self._index = None
        self.last_error = None

        self.sio.on("state", self._on_state)
        self.sio.on("create_pro_controller", self._on_created)
        self.sio.on("error", self._on_error)

    # ---------- lifecycle ----------

    def connect(self):
        self.sio.connect(
            self.url,
            transports=["polling"],
            wait_timeout=10,
        )
        return self

    def close(self):
        if self.sio.connected:
            self.sio.disconnect()

    # ---------- event handlers ----------

    def _on_state(self, data):
        self.state = data
        self._state_event.set()

    def _on_created(self, index):
        self._created_index = int(index)
        self._created_event.set()

    def _on_error(self, message):
        self.last_error = message
        print(f"[webapp error] {message}")

    # ---------- state ----------

    def refresh_state(self, timeout=2.0):
        """Ask the webapp for state and wait for the reply."""
        self._state_event.clear()
        self.sio.emit("state")
        if not self._state_event.wait(timeout):
            raise TimeoutError("no state event from webapp")
        return self.state

    def connected_index(self):
        # Cache the index: QTE loops must not do a state round-trip per prompt.
        if self._index is not None:
            return self._index
        state = self.refresh_state()
        for key, value in state.items():
            if value.get("state") == "connected":
                self._index = int(key)
                return self._index
        raise RuntimeError(f"no connected controller: {state}")

    def wait_connected(self, index=None, timeout=120.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.refresh_state()
            if index is None:
                for key, value in state.items():
                    if value.get("state") == "connected":
                        self._index = int(key)
                        return self._index
            else:
                info = state.get(str(index), {})
                if info.get("state") == "connected":
                    self._index = int(index)
                    return self._index
                if info.get("state") == "crashed":
                    raise RuntimeError(info.get("errors"))
            time.sleep(0.1)
        raise TimeoutError("controller did not reach connected")

    # ---------- controller creation (Mode B only) ----------

    def create_controller(self, timeout=60.0):
        self._created_event.clear()
        self.sio.emit("web_create_pro_controller")
        if not self._created_event.wait(timeout):
            raise TimeoutError("controller creation timed out")
        self._index = self._created_index
        return self._created_index

    # ---------- macros ----------

    def macro(self, text, index=None, timeout=30.0):
        """Run a macro and wait for it to finish."""
        index = index if index is not None else self.connected_index()
        macro_id = self.sio.call(
            "macro",
            json.dumps([int(index), text]),
            timeout=5,
        )
        if not self.wait_macro(macro_id, index=index, timeout=timeout):
            raise TimeoutError(f"macro did not finish: {macro_id}")
        return macro_id

    def macro_async(self, text, index=None):
        """Fire-and-forget macro, useful for fast QTE prompts."""
        index = index if index is not None else self.connected_index()
        self.sio.emit("macro", json.dumps([int(index), text]))

    def wait_macro(self, macro_id, index=None, timeout=30.0):
        index = index if index is not None else self.connected_index()
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.refresh_state()
            info = state.get(str(index), {})
            if macro_id in info.get("finished_macros", []):
                return True
            time.sleep(0.03)
        return False

    def stop_all(self):
        self.sio.emit("stop_all_macros")

    # ---------- direct input (optional) ----------

    def idle_packet(self, index=None):
        """Build a full idle packet.

        state["0"]["direct_input"] may still contain the last direct input,
        so explicitly clear every button/stick before sending.
        """
        index = index if index is not None else self.connected_index()
        state = self.refresh_state()
        packet = copy.deepcopy(state[str(index)]["direct_input"])

        for key, value in packet.items():
            if isinstance(value, bool):
                packet[key] = False

        for stick_name in ("L_STICK", "R_STICK"):
            stick = packet[stick_name]
            stick["PRESSED"] = False
            stick["X_VALUE"] = 0
            stick["Y_VALUE"] = 0
            for key in list(stick):
                if key.startswith("LS_") or key.startswith("RS_"):
                    stick[key] = False

        return packet

    def send_input(self, packet, index=None):
        """Send one 120 Hz protocol cycle. For a held input, use a macro
        or send repeatedly at ~120 Hz."""
        index = index if index is not None else self.connected_index()
        self.sio.emit("input", json.dumps([int(index), packet]))

    def shutdown(self, index):
        self.sio.emit("shutdown", int(index))
        if self._index == int(index):
            self._index = None


if __name__ == "__main__":
    # 把 <PI_IP> 换成 Pi 的局域网地址
    client = SwitchClient("http://<PI_IP>:8000").connect()
    try:
        state = client.refresh_state()
        print(json.dumps(state, indent=2, ensure_ascii=False))

        # 如果 Switch 还在 Change Grip/Order，这里会等待配对完成
        index = client.wait_connected(timeout=120)
        print(f"controller index = {index}")

        # 无害测试：只等待 100ms，不按任何键
        client.macro("0.1s", index=index)
        print("Socket.IO bridge OK")
    finally:
        client.close()
```

### 4.5 宏语法

nuxbt 的宏是逐行文本，格式：

```text
<按键...> <持续时间>
```

- 持续时间必须以 `s` 结尾，例如 `0.1s`、`1.5s`。
- 只有时间表示等待，例如 `0.5s`。
- 多个按键用空格分隔，例如 `A B 0.1s`。
- 行首 `#` 是注释。
- 空行会被忽略。
- **按键名必须大写**。

常用按键名：

| 类别 | 名称 |
|---|---|
| 面键 | `A` `B` `X` `Y` |
| 方向键 | `DPAD_UP` `DPAD_DOWN` `DPAD_LEFT` `DPAD_RIGHT` |
| 肩键/扳机 | `L` `R` `ZL` `ZR` |
| 摇杆按下 | `L_STICK_PRESS` `R_STICK_PRESS` |
| 系统键 | `PLUS` `MINUS` `HOME` `CAPTURE` |
| 左摇杆 | `L_STICK@+100+000` 等 |
| 右摇杆 | `R_STICK@+000-100` 等 |

摇杆格式是：

```text
L_STICK@<X符号><3位X><Y符号><3位Y>
```

例如：

- `L_STICK@+100+000`：左摇杆最右
- `L_STICK@-100+000`：左摇杆最左
- `L_STICK@+000+100`：左摇杆最上
- `L_STICK@+000-100`：左摇杆最下
- `L_STICK@+070+070`：右上斜向

> 摇杆方向的正负需要在实际游戏里确认一次。建议先用很短的时间（`0.1s`）测试，避免角色跑远。

循环：

```text
LOOP 3
    A 0.1s
    0.5s
    B 0.1s
    0.5s
```

- `LOOP` 必须顶格写。
- 循环体用 **4 个空格**缩进（也支持 tab，但推荐 4 空格）。
- 循环体结束于第一行不缩进的行。

长按/组合键：

```text
HOLD ZL
    A 0.1s
    0.2s
```

表示在按住 `ZL` 的同时按 `A`。如果只是单纯长按某个键，直接写持续时间：

```text
Y 1.0s
```

这会在 1 秒内保持 `Y` 按下，适合给浇水壶蓄力（具体蓄力时长需要按游戏实测调整）。

**示例：浇水条带**

```text
# 面向地块，蓄力 1.2 秒后松手
Y 1.2s
0.8s
# 向右移动一小步
L_STICK@+100+000 0.35s
L_STICK@+000+000 0.1s
0.3s
```

**示例：钓鱼 QTE 单次响应**

```text
A 0.05s
```

QTE 时建议用 `macro_async()` 发送，不要等待宏完成，否则会阻塞视觉循环。  
`SwitchClient` 会缓存 controller index，QTE 循环里只取一次 index，不要每帧刷新 `state`。

### 4.6 直接输入包（可选）

`input` 事件用于连续摇杆/高频输入，但第一版不建议用它做按键，原因：

- 一次 `input` 只发送**一个协议周期**。
- 要按住某个键 100ms，需要以约 120Hz 连续发送 12 个包。
- 宏更简单：`A 0.1s` 就是按住 100ms。

如果确实要用：

```python
packet = client.idle_packet(index)
packet["A"] = True
client.send_input(packet, index=index)
```

- `packet` 必须包含完整字段，推荐从 `state["0"]["direct_input"]` 深拷贝。
- 摇杆 `X_VALUE` / `Y_VALUE` 服务端按 `value / 100` 计算比例，所以用 **-100 ~ 100**，`100` 表示满舵。不要直接照搬网页前端的 `32767`。
- `input` 不带 ack；发送后不会等待。

### 4.7 停止与急停

Mac 端：

```python
client.stop_all()          # 停止所有正在跑/排队的宏
client.shutdown(index)     # 移除控制器（模式 B 才用；模式 A 不要用）
```

Pi 端硬急停：

```bash
./scripts/switchctl.sh stop
```

这会终止 webapp/test，控制器也会断开。最坏情况下关闭 webapp 或断开蓝牙。

建议在 Mac 脚本里加一个全局快捷键/终端输入，随时调用 `stop_all()`；未知画面停留超过 3 秒也自动 `stop_all()` 并保存截图。

### 4.8 第一次连接测试

1. Pi 上确认 webapp 已启动，浏览器已创建控制器，状态 `connected`。
2. Mac 上运行 `switch_client.py`，把 `<PI_IP>` 换成实际地址。
3. 看到 `state` 里有 `"state": "connected"`。
4. 发送 `client.macro("0.1s")`，确认不报错。
5. 再在安全界面（主菜单/空地）发送 `A 0.1s`，确认游戏有反应。
6. 记录一次 `macro` 的往返时间，作为后续 QTE 延迟基线。

---

## 5. Mac 视觉环境

### 5.1 硬件连接

```text
Switch 2 底座 ──HDMI──> 采集卡 HDMI IN
采集卡 HDMI OUT ──> 电视/显示器（如果有环出）
采集卡 USB ──> Mac USB 3.0 端口
```

- 尽量直接插 Mac 的 USB 3 端口，不要经过廉价 USB 2 Hub。
- 采集卡最好支持 **1080p60**，并且是 **UVC 免驱**。
- Switch 2 设置：
  - TV 输出分辨率：**1080p**
  - HDR：**关闭**
  - 如果采集卡黑屏，先检查 HDCP/HDR/分辨率。
- 如果采集卡没有 HDMI 环出，加一个 HDMI 分配器，否则电视和采集卡不能同时用。

### 5.2 macOS 权限

macOS 会把采集卡当成摄像头。第一次运行 OpenCV 时必须在：

**System Settings → Privacy & Security → Camera**

允许 **Terminal / iTerm / VS Code** 访问相机。否则 `cap.isOpened()` 可能返回 `False`，或者读到黑屏。

### 5.3 Python 环境

```bash
cd switch2RemoteCtl

python3 -m venv .venv-mac
source .venv-mac/bin/activate

python -m pip install -U pip
pip install opencv-python numpy python-socketio requests pyyaml
```

可选（后面做菜单 OCR 再装，不要一开始装太多）：

```bash
# 中文 OCR
brew install tesseract tesseract-lang
pip install pytesseract
```

> Apple Silicon 上 `opencv-python` 有原生 wheel。  
> 不建议一上来装 PaddleOCR/PaddlePaddle，依赖重且 macOS ARM 兼容性容易出问题；先用模板匹配和颜色阈值。

### 5.4 采集卡枚举

```bash
ffmpeg -f avfoundation -list_devices true -i ""
```

输出里会列出视频设备，例如：

```text
[0] FaceTime HD Camera
[1] USB Video
```

OpenCV 的 index 可能和 ffmpeg 的 index 不完全一致；从 0、1、2 依次试。

也可以用：

```bash
system_profiler SPUSBDataType | grep -A 8 -iE "capture|hdmi|video"
```

确认采集卡被 macOS 识别。

### 5.5 采集卡自检（Mac）

保存为 `vision/capture_test.py`：

```python
#!/usr/bin/env python3
"""Capture card smoke test: resolution, FPS, and a reference frame."""

import time

import cv2


def main(index=0):
    cap = cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
    if not cap.isOpened():
        raise SystemExit(
            f"cannot open camera index {index}; "
            "check macOS Camera permission and try another index"
        )

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
    cap.set(cv2.CAP_PROP_FPS, 60)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    print(
        "reported:",
        int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
        "x",
        int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        "@",
        cap.get(cv2.CAP_PROP_FPS),
    )

    frames = 0
    started = time.time()
    last_report = started

    while True:
        ok, frame = cap.read()
        if not ok:
            print("read failed")
            break

        frames += 1
        now = time.time()

        if now - last_report >= 1.0:
            fps = frames / (now - started)
            print(f"actual fps ≈ {fps:.1f}, frame={frame.shape}")
            last_report = now

        cv2.imshow("capture (ESC to quit)", frame)
        if cv2.waitKey(1) & 0xFF == 27:
            break

    if frames:
        cv2.imwrite("reference.png", frame)
        print("saved reference.png")

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main(0)
```

运行：

```bash
python vision/capture_test.py
```

验收标准：

- 分辨率 1920×1080。
- 实际 FPS ≥ 55（目标 60）。
- 画面稳定 10 分钟，不黑屏、不卡顿。
- `reference.png` 能清楚看到游戏 UI 和农场。

如果只有 30 FPS：

1. 确认采集卡支持 1080p60。
2. 换 USB 3 端口，去掉 Hub。
3. 在 `ffmpeg` 里确认设备支持的格式。
4. 某些廉价卡只支持 1080p30；浇水够用，钓鱼 QTE 会比较吃力。

### 5.6 低延迟采集

**不要用 OBS 虚拟摄像头做运行时输入**，会多一层缓冲和延迟。调试时可以用 OBS 看画面，但 Python 直接抓 UVC 设备。

OpenCV 的 `cap.read()` 如果处理慢，会积压旧帧。推荐一个“只保留最新帧”的采集线程：

```python
import threading
import time

import cv2


class LatestFrame:
    def __init__(self, index=0):
        self.cap = cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
        if not self.cap.isOpened():
            raise RuntimeError("capture card not opened")

        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        self.cap.set(cv2.CAP_PROP_FPS, 60)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        self._frame = None
        self._lock = threading.Lock()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while self._running:
            ok, frame = self.cap.read()
            if not ok:
                time.sleep(0.01)
                continue
            with self._lock:
                self._frame = frame

    def read(self):
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    def close(self):
        self._running = False
        self._thread.join(timeout=1.0)
        self.cap.release()
```

QTE 时只处理屏幕的一小块 ROI，例如 400×200，而不是整张 1080p 图。

### 5.7 Mac 端目录建议

```text
vision/
├── capture_test.py
├── switch_client.py
├── capture.py          # LatestFrame
├── calibrate.py        # 框 ROI / 保存模板
├── config.yaml         # ROI、阈值、路径
├── templates/
│   ├── ui/             # 菜单、按钮提示、对话框
│   ├── farm/           # 土壤、作物、体力
│   └── fishing/        # 浮标、咬钩、QTE
└── tasks/
    ├── watering.py
    ├── fertilizing.py
    ├── harvesting.py
    ├── crafting.py
    └── fishing.py
```

---

## 6. 视觉识别设计

### 6.1 标定流程

1. 把游戏停在稳定画面（农场、钓鱼点、风车菜单）。
2. 运行 `capture_test.py` 保存 `reference.png`。
3. 用 `cv2.selectROI()` 框出：
   - 体力条
   - 金币/时间
   - 土壤检测区
   - 钓鱼 QTE 提示区
   - 对话框/背包区域
4. 把 ROI 保存成**归一化坐标**（0–1），避免以后改分辨率：
   ```yaml
   rois:
     stamina: [0.03, 0.05, 0.18, 0.025]
     fishing_prompt: [0.35, 0.72, 0.30, 0.18]
   ```
5. 从参考图裁剪模板，保存到 `templates/`。
6. 写一个 `detect` 脚本，实时打印置信度；阈值先设 0.8，再按实际调整。

> 3D 游戏的光照/天气会变。颜色阈值不要写死 RGB，优先用 HSV 的色相/饱和度范围，或者用模板匹配 + 多帧确认。

### 6.2 通用状态机

```python
class VisionTask:
    def __init__(self, switch, source, config):
        self.switch = switch
        self.source = source
        self.config = config
        self.running = True

    def detect(self, frame):
        raise NotImplementedError

    def decide(self, state):
        raise NotImplementedError

    def run(self):
        while self.running:
            frame = self.source.read()
            if frame is None:
                continue

            state = self.detect(frame)
            action = self.decide(state)

            if action:
                self.switch.macro(action)

            time.sleep(1 / 30)
```

原则：

- 每个任务有明确的 **precondition / state / action / abort condition**。
- 任何动作都要有超时；未知画面超过 3 秒就暂停并保存截图。
- 不要写无限循环的固定宏；至少要有视觉检查点。
- 先“只识别、不按键”跑通，再开动作。

### 6.3 任务优先级

| 优先级 | 任务 | 难度 | 采集卡 | 备注 |
|---|---|---|---|---|
| P0 | 环境 + Socket 桥接 | 低 | 需要 | 先验证 Mac 能控制 Pi |
| P1 | 自动浇水 | 中 | 建议 | 农场固定、浇水范围大，最适合第一个闭环 |
| P2 | 自动施肥 | 中 | 建议 | 复用浇水路线，增加肥料数量检测 |
| P3 | 自动收获 | 中高 | 建议 | 作物成熟识别、背包容量 |
| P4 | 风车/料理制作 | 低–中 | 可选 | 菜单宏 + OCR/模板校验 |
| P5 | 自动钓鱼 | 高 | **必须** | QTE 对延迟敏感，放最后 |

### 6.4 自动浇水（P1）

**已确认的机制**：这作浇水壶有 Super Water / Ultra Water 蓄力范围，基础 3×1 / 3×2 格，升级后更大。因此不需要一格一格点，可以按 3 格宽的条带规划。

**前置条件**：

- 角色在农场，面向地块。
- 浇水壶已装备。
- 体力高于阈值（例如 30%）。
- 水壶有足够水量，或知道补水点路线。

**状态机**：

```text
IDLE
  -> 检查体力/水壶
  -> 走到条带起点
  -> 面向地块
  -> 蓄力浇水（Y 按住 N 秒）
  -> 检查土壤是否变湿
  -> 移动到下一条带
  -> 全部完成 / 体力低 / 水壶空 -> DONE / REFILL / ABORT
```

**视觉检查**：

- 土壤干/湿：在条带中心取小 ROI，比较 HSV 的明度/饱和度；也可以保存“干土/湿土”模板做匹配。
- 体力：HUD 体力条 ROI，计算填充比例；低于阈值就停。
- 水壶空：检测 UI 提示或角色动作；也可以按“已浇水次数”估算容量。
- 下雨天：土壤本来就是湿的，视觉检查会自动跳过。

**导航**：

- 先固定游戏相机角度，不要手动转 R 摇杆。
- 用“撞墙/撞栅栏”重置角色位置，再按固定步长走。
- 步长用摇杆宏控制，例如 `L_STICK@+000-100 0.35s`，需要实测校准。
- 如果农场有明确边界，优先用边界对齐，避免累积误差。

**第一版验收**：

- 3×3 小农场，90% 以上地块变湿。
- 体力低时能停。
- 水壶空时能停在原地并截图，不会乱跑。
- 连续跑 10 次不出错。

### 6.5 自动施肥（P2）

- 先手动录一段施肥，确认：
  - 肥料是从背包选中后按 `Y` 使用，还是用专门工具。
  - 是单格还是范围效果。
  - 肥料是否每天/每阶段只能用一次。
- 复用浇水的网格路线。
- 视觉检查：
  - 肥料剩余数量（OCR 或图标计数）。
  - 地块是否已施肥（如果有图标/颜色变化）。
- 肥料为 0 时停止；不要自动购买（除非以后单独做采购任务）。

### 6.6 自动收获（P3）

- 先只支持 **一种作物**，不要一上来做全作物。
- 成熟识别：作物模板/颜色/发光特效；每种作物单独模板。
- 路线：同一块地按行/列走，按 `A` 收获，等待动画。
- 背包满：
  - 第一版检测到“背包满”对话框就暂停。
  - 第二版再做出货箱路线和出货菜单。
- 再生作物：只摘成熟果实，不要按 `A` 多次导致拔起。

> “采集作物”如果指的是地图上捡野果/矿石，而不是收菜，路线和识别会完全不同；需要单独确认。

### 6.7 风车/料理制作（P4）

这是最适合先做纯宏的任务：

1. 走到风车/料理台。
2. 交互，打开菜单。
3. 选择配方（模板/OCR/固定光标）。
4. 设置数量。
5. 确认。
6. 等待完成（游戏内时间）。
7. 取货。

视觉检查点：

- 配方名称/图标是否正确。
- 材料是否足够；出现“材料不足”就中止。
- 数量是否正确。
- 完成后的取货提示。

如果菜单会记住上次配方，宏可以很短；如果每次重置，需要 OCR/模板定位。建议先用网页的 **Record** 功能录一遍手动操作，导出宏，再加视觉校验。

> “制作东西”需要确认是风车加工、料理，还是两者都要。

### 6.8 自动钓鱼（P5）

**机制**：咬钩后是按键 QTE，屏幕会提示按钮顺序；不是简单“浮标下沉按 A”。

**状态机**：

```text
IDLE
  -> 走到钓鱼点，面向水面
  -> 装备鱼竿
  -> 抛竿
  -> WAIT_BITE（超时重抛）
  -> 咬钩 -> 按游戏提示的收杆键（通常是 A，以录屏为准）
  -> QTE_LOOP:
       识别当前提示按钮（A/B/X/Y）
       发送对应短宏（0.05s）
       如果出现 Reel Power -> 按住提示键
  -> 成功/失败
  -> 背包满？-> 停止
  -> 否则继续抛竿
```

**视觉识别**：

- 浮标/鱼线：颜色/模板。
- 咬钩：浮标下沉、水花、`!` 图标；用多帧确认，避免误触。
- QTE 提示：屏幕底部/中央的按钮图标，分别做 A/B/X/Y 模板；只处理 ROI。
- 用 `macro_async()` 发送，不要等宏完成。
- 失败/脱钩：检测结果画面，重抛。

**延迟要求**：

- 采集 60 FPS。
- ROI 处理 < 10 ms。
- 不要用 OBS 虚拟摄像头。
- 局域网优先有线。
- 如果 QTE 窗口很窄，廉价采集卡（100–200 ms）可能不够，需要 Elgato HD60 X / Cam Link 4K / AVerMedia 低延迟卡。

**第一版验收**：

- 20 次抛竿，成功 ≥ 16 次（80%）。
- 背包满/鱼竿未装备/体力低时能停。
- 连续跑 30 分钟不崩。

### 6.9 每日循环（P6，后续）

等单个任务稳定后再组合：

```text
起床
  -> 检查天气/体力
  -> 浇水（下雨跳过）
  -> 施肥（按计划）
  -> 收获
  -> 风车/料理
  -> 钓鱼
  -> 出货/睡觉
```

需要额外识别：

- 游戏内时间/日期
- 天气
- 体力
- 背包
- 节日/市集日（第一版遇到就暂停）

---

## 7. 里程碑与验收

### M0：Mac 采集环境

- [ ] 采集卡识别为 UVC。
- [ ] 1920×1080 @ ≥55 FPS，稳定 10 分钟。
- [ ] 保存 `reference.png`。
- [ ] 测一次采集延迟（60fps 秒表 + 手机慢动作，或查采集卡规格）。

### M1：Socket 桥接

- [ ] Mac 能连接 `http://<PI_IP>:8000`，`transports=["polling"]`。
- [ ] 能读到 `connected` 状态和 controller index。
- [ ] 能发送 `0.1s` 无害宏。
- [ ] 能在安全界面发送 `A 0.1s` 并看到游戏反应。
- [ ] 能调用 `stop_all()`。

### M2：自动浇水 MVP

- [ ] 只识别、不按键，能判断干/湿土和体力。
- [ ] 3×3 小农场浇水成功 ≥90%。
- [ ] 体力低/水壶空能停。
- [ ] 连续 10 次无异常。

### M3：施肥/收获

- [ ] 一种作物成熟识别。
- [ ] 背包满能停。
- [ ] 不误拔未成熟作物。

### M4：风车/料理

- [ ] 一个配方连续制作 10 次，材料/数量正确。
- [ ] 材料不足能停。

### M5：钓鱼

- [ ] 咬钩检测准确。
- [ ] QTE 按钮识别准确。
- [ ] 20 次成功率 ≥80%。

### M6：每日循环

- [ ] 连续 3 个游戏日，人工监督下完成。
- [ ] 异常能暂停并保存截图。

---

## 8. 风险与限制

1. **采集卡延迟**：钓鱼 QTE 可能对廉价卡不友好；先测延迟再决定是否升级。
2. **配对/MAC 轮换**：每次新会话可能需要重新配对；不要把 `enable` 和 webapp 同时跑。
3. **控制器生命周期**：模式 A 依赖浏览器标签页；模式 B 需要改 webapp 才能长期稳定。
4. **webapp 无鉴权**：只允许可信局域网；不要端口映射到公网。
5. **游戏更新**：UI/菜单/图标可能变；模板需要重新标定。
6. **3D 光照/天气**：颜色阈值会漂移；优先模板匹配 + 多帧确认。
7. **Switch 2 睡眠**：关闭自动睡眠，保持底座供电；否则控制器会断。
8. **存档风险**：先备份；先小范围测试；不要在重要存档上直接跑全自动。
9. **条款风险**：只做离线/单人自动化；不要用于在线对战、排名或交易。
10. **公开仓库**：文档和代码不要写内网 IP、用户名、密钥；用 `<PI_IP>` / `<USER>` 占位。

---

## 9. 需要补充的信息

在开始写第一个任务前，需要确认：

1. **Mac 硬件**：Apple Silicon 还是 Intel？macOS 版本？
2. **采集卡**：型号、是否支持 1080p60、是否有环出、USB 3.0？
3. **游戏进度**：
   - 浇水壶等级/蓄力范围
   - 农场大概多少格、形状
   - 用的什么肥料、是否范围施肥
   - 钓鱼竿等级、是否有 Reel Power
   - 体力上限
4. **录屏/截图**（手机拍屏也行，先做方案）：
   - 农场全景
   - 手动浇水一次（含蓄力）
   - 施肥一次
   - 收获一次
   - 钓鱼咬钩 + QTE
   - 风车/料理菜单
   - 背包/体力条
5. **术语确认**：
   - “采集作物”是收菜，还是地图捡野果/矿石？
   - “制作东西”是风车加工、料理，还是两者？
6. **先做哪个任务**：建议自动浇水；如果你主要想挂机钓鱼，也可以先做钓鱼 QTE 原型。
7. **存档备份**：是否已开 Nintendo Switch Online 云备份，或愿意先在小地块测试？

---

## 10. 参考

- 游戏操作（A 交互、Y 使用、B 跳、X 背包、L 走/跑）：
  <https://game8.co/games/Story-of-Seasons-Grand-Bazaar/archives/543663>
- 浇水壶范围/升级：
  <https://game8.co/games/Story-of-Seasons-Grand-Bazaar/archives/545680>
- 钓鱼竿与 QTE：
  <https://game8.co/games/Story-of-Seasons-Grand-Bazaar/archives/545685>
- 自然精灵（应援/商店，不是自动农活）：
  <https://game8.co/games/Story-of-Seasons-Grand-Bazaar/archives/546636>
- nuxbt 上游：<https://github.com/hannahbee91/nuxbt>
- 本项目：<https://github.com/lilong7676/switch2RemoteCtl>

---

## 附录 A：宏速查

```text
# 等待
0.5s

# 单键
A 0.1s

# 多键同时
A B 0.1s

# 长按蓄力
Y 1.2s

# 左摇杆向右 0.35 秒
L_STICK@+100+000 0.35s

# 左摇杆回中
L_STICK@+000+000 0.1s

# 循环 3 次
LOOP 3
    A 0.1s
    0.5s

# 按住 ZL 的同时按 A
HOLD ZL
    A 0.1s
    0.2s
```

注意：

- 按键名大写。
- 每行必须有持续时间（`...s`）。
- `LOOP` 顶格，循环体 4 空格。
- 摇杆宏先用 0.1s 短测，确认方向再加大。

---

## 附录 B：常见问题

### 采集卡打不开

- macOS 相机权限是否给了 Terminal/IDE。
- `ffmpeg -f avfoundation -list_devices true -i ""` 看设备号。
- 换 USB 3 端口，去掉 Hub。
- 关掉其他占用摄像头的应用（OBS、Zoom、QuickTime）。

### 只有 30 FPS

- 采集卡规格是否 1080p60。
- USB 是否 3.0。
- Switch 2 输出是否 1080p60。
- 某些卡在 1080p 下只给 30fps；降到 720p60 试试。

### Socket 连不上

- Pi 上 webapp 是否在跑：`curl http://<PI_IP>:8000/`。
- 是否用了 `transports=["polling"]`。
- Mac 和 Pi 是否同一局域网；防火墙是否放行。
- 不要用 `https://`，除非 webapp 配了证书。

### `state` 里没有控制器

- 浏览器是否点了 Create。
- 控制器是否还在 `connecting`；Switch 是否在 Change Grip/Order。
- 是否只有一块蓝牙适配器；第二个控制器会创建失败。

### 宏没反应

- controller index 是否取对（`state` 的 key 是字符串）。
- 宏是否大写、每行是否有 `s`。
- 控制器是否 `connected`。
- 是否被 `stop_all_macros` 清掉了。

### 页面一直 connecting / Authentication Failure

```bash
./scripts/switchctl.sh stop
./scripts/switchctl.sh newmac
./scripts/switchctl.sh webapp
```

然后 Switch 2 重新进 Change Grip/Order 配对。

### Mac 睡眠导致断连

```bash
caffeinate -dimsu
```

或在系统设置里关闭自动睡眠；长时间任务建议接电源。

---

## 附录 C：下一步

1. Mac 按第 5 节搭好环境，跑通 `capture_test.py`。
2. Mac 按第 4.4 节跑通 `switch_client.py`，确认能发 `0.1s` 宏。
3. 把采集卡型号、Mac 芯片、游戏截图/录屏发回来。
4. 先做 **自动浇水** 的“只识别不按键”版本，再开动作。
5. 浇水稳定后，按 P2 → P3 → P4 → P5 推进。

