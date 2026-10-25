---
name: switch-vision
description: Use when working on the Switch 2 farming-game vision pipeline in this repo — capturing frames from the nuxbt bridge through OBS, gating on UI mode (world / warehouse / backpack), reading inventory grids (item icons, quality stars, stack counts, prices), learning new items into the catalog, or debugging why a grid cell, badge, or count is misread.
---

# Switch 2 inventory vision

本项目（`switch2RemoteCtl`，分支 `grand-bazaar`）的视觉识别流水线。目标：**一张截图读懂整屏物品栏**，
不再逐格移动光标。当前成绩：9 页 198 格仓库 + 24 格背包 = 222 格，
**物品识别 222/222、数量 222/222、星级 187/187（有角标的格子全部读对）**。

详细数值配方见 `references/recognition-recipes.md`（所有裁剪框、阈值、遮罩比例都在那里）。

## 数据流

```text
OBS obs-websocket ──screenshot──> state_reader (模式门禁)
     1920x1080                       ├─ HOME / WORLD / MODAL / WAREHOUSE / BACKPACK
                                     ├─ WORLD 才允许动输入（world_is_safe）
                                     ▼
                              grid_reader (格子读取)
                                     ├─ cells()      格子几何 + 占用 + 选中
                                     ├─ patch()      遮罩图标指纹
                                     ├─ badge()      品质星级（14 档 0.5–7）
                                     ├─ count()      堆叠数量
                                     └─ ItemCatalog  持久化目录（图标指纹 + 基准价）
                                     ▼
                              scan_inventory.py ──> data/inventory_full.json
```

## 环境与配置

- 运行环境：`.venv-mac/bin/python`（macOS，OpenCV + websockets）。
- 画面来源：OBS 31.x 自带 obs-websocket，`ws://127.0.0.1:4455`。
  **密码不提交**：读 `OBS_WS_PASSWORD` 环境变量，或 `vision/local_config.json`
  （gitignored，模板见 `vision/local_config.example.json`）。
- 控制器：Pi 5 上的 nuxbt webapp `http://192.168.5.19:8000`，浏览器持有手柄（Mode A），
  Mac 只读 `state` + 发 `macro` 字符串。控制器索引**不要硬编码**，用 `wait_connected()` 发现。
- 观察/调试单帧：`.venv-mac/bin/python vision/live_probe.py`。

## 工作流 A：整仓盘点（最常用）

```bash
.venv-mac/bin/python vision/scan_inventory.py --open    # 自动开仓库、逐页扫描
```

- 会先 `B` 关 + `A` 开重置（光标和页码都回 1），再用 `R` 翻页，逐页读右网格；左背包只读一次。
- 每页输出 `unknown=/badges=/stars=`，结果写 `vision/data/inventory_full.json`。
- 扫描时 `harvest=True` 会自动积累**角标模板**（见下），越用越准。
- 整仓 9 页约 4–5 分钟（每格 badge+count 两次 OCR）。

## 工作流 B：学新物品 / 补格子

```bash
# 全量学习一个网格（背包 24 格 / 仓库某页 24 格）；会读面板名称/描述/价格 + 角标 + 数量 + 指纹
.venv-mac/bin/python vision/learn_inventory.py --arm --grid backpack  --cells 24
.venv-mac/bin/python vision/learn_inventory.py --arm --grid warehouse --page 3 --cells 24

# 只补"扫出来是 unknown"的格子：先聚类未知图标，只走每个聚类的一个代表格
.venv-mac/bin/python vision/learn_unknown.py --arm

# 指定格子补学（带验证式导航，适合个别顽固格子）
.venv-mac/bin/python vision/learn_targets.py --arm --targets 5:3:3 8:2:1
.venv-mac/bin/python vision/learn_targets.py --arm --unknown
```

- 仓库进入方式（确定性，已实测）：重开仓库（`B` 再 `A`）→ 按 `R`×（页码-1）→ 按 `RIGHT`×8
  进入右网格 `(0,0)`。
- **不要用长路径盲走**：方向键偶发丢帧会让后续格子全部错位（曾把 7 个格子学成"学过但仍 unknown"）。
  要么用 `learn_targets.py` 的验证式导航，要么每次用物品名验证（`learn_prices.py` 的做法）。

## 工作流 C：补价格

```bash
.venv-mac/bin/python vision/learn_prices.py --arm --missing
```

- 按页分组、相对移动、**用物品名验证导航**（期望名字来自上一次盘点结果）。
- 价格模型：`售价 = 0.5★基准价 × 倍率(品质)`，14 档固定倍率表（见 `item_prices.py`）。
  **一个物品只要观测到任意一档价格，就能推导全部 14 档**，不必逐档学习。
- 没有角标的物品（瓶子/靴子/鱼骨/空罐等）售价就是基准价。

## 铁律（都是踩过的坑）

1. **图标指纹要遮四角**：学习时格子是选中态，蓝色选中框会混进指纹。遮罩 = 顶部 32%（角标）
   + 左下角 + 右下角（数量）。否则"同一格选中/未选中"只有 0.90 相似度，阈值 0.86 会漏。
2. **裁剪要从整帧绝对坐标算**：角标和数量是**溢出格子边界**的（角标 `dy=-13`），按 cell 内裁剪会切掉字形。
3. **OCR 语言必须 `en-US`**：默认 `zh-Hans,en-US` 会把 `0.5` 读成 `回5`。中文名称/描述仍用默认。
4. **彩虹星 = 品质 7**（24/24 实测）：先看星图标颜色，rainbow 直接返回 7，只有蓝色才走 OCR。
   但**蓝色星覆盖所有非彩虹品质**（5.0 也可能是蓝星），不要按颜色过滤候选值。
5. **数量空结果 ⇒ 1**：Vision 检测不到单个窄 `1`，而 1 是默认堆叠。
6. **单字形角标用模板兜底**：`badge_templates.py` 的字形归一化（去卡片横线、裁星星残影、Otsu 取最大连通域、
   按高度归一化到 40×28）比直接 OCR 稳。扫描时持续采集模板。
7. **区分"没有角标"和"读不出"**：`badge.present=False` 是合法状态（垃圾/装饰类物品），不要当失败。
8. **网格几何必须实测**：真实间距 106×111（卡片 93×92），首格 (149,268) / (1144,268)；
   按 ROI 均分会漂移到最后一列偏 ~40px。改 UI 分辨率后必须重新标定。
9. **PATH 上别发 `A` 除非有前置校验**：开仓库前用模板 + OCR 复核提示文本含「仓库」；
   连接后第一个宏可能丢失，`A` 要允许重试。
10. **错帧不要污染 OCR**：标注文字会压在 HUD 上导致误读，干净帧和标注帧分开存。
11. **socket 会抽风**：`macro_ack` 已内置 3 次重试 + 自动重连；扫描中断后重跑即可。
12. **不要硬编码 OBS 密码 / 控制器索引**：密码走 `local_config.json`，索引用 `wait_connected()`。

## 数据与文件

| 路径 | 说明 | 是否提交 |
|---|---|---|
| `vision/data/item_catalog.json` | 物品目录：名称/别名/有点评/基准价/图标指纹（base64） | ✅ |
| `vision/data/badge_templates.json` | 角标数字模板池 | ✅ |
| `vision/data/inventory_full.json` | 最近一次整仓盘点结果 | ✅ |
| `vision/data/learning_*.jsonl`、`learn_prices_*.jsonl` | 学习日志 | ❌ gitignore |
| `vision/data/learning_frames/`、`vision/logs/`、`vision/samples/` | 截图与过程帧（几十 MB） | ❌ gitignore |
| `vision/templates/m1/*.png` | 模式判定用小模板（必须提交） | ✅ |

## 改动后的验证

1. 离线：拿一张存档帧跑 `GridReader.scan(frame, "WAREHOUSE")`，看 unknown 数量与相似度分布；
   `vision/samples/warehouse_scan_1918.png` 有 24 格人工标注（角标/数量真值）。
2. 在线：`scan_inventory.py --open` 看每页 `unknown=0`、`badges == stars`。
3. 价格：`python vision/item_prices.py` 会打印 14 档倍率表和往返校验。
4. 角标模板：`python vision/badge_templates.py` 打印各品质的模板数量。

更长的背景、时间线和失败案例都在 `docs/MAC_HANDOFF.md`（§12.x）。
