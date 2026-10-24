#!/usr/bin/env bash
# 重新安装 nuxbt 后，用本脚本恢复本地 patch。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SITE="${1:-}"
if [ -z "$SITE" ]; then
  if [ -f "$ROOT/nuxbt/nuxbt/cli.py" ]; then
    SITE="$ROOT/nuxbt/nuxbt"
  else
    SITE="$ROOT/.venv/lib/python3.13/site-packages/nuxbt"
  fi
fi
CLI="$SITE/cli.py"

if [ ! -f "$CLI" ]; then
  echo "找不到 $CLI" >&2
  echo "用法: $0 [nuxbt 包目录]" >&2
  exit 1
fi

"$ROOT/.venv/bin/python" - "$SITE" <<'PY'
import re
import sys
from pathlib import Path

site = Path(sys.argv[1])

# 1) CLI: -a/--address 必须是字符串，而不是布尔 flag
cli = site / "cli.py"
text = cli.read_text()
old = """@click.option('-a', '--address', required=False, default=False,
              help=\"nuxbt will attempt to reconnect to a specific Bluetooth MAC address of a Nintendo Switch.\")"""
new = """@click.option('-a', '--address', required=False, default=None, type=str,
              help=\"nuxbt will attempt to reconnect to a specific Bluetooth MAC address of a Nintendo Switch.\")"""
if new in text and old not in text:
    print("already patched:", cli)
elif old in text:
    cli.write_text(text.replace(old, new))
    print("patched:", cli)
else:
    raise SystemExit(f"pattern not found in {cli}; nuxbt version may have changed")

# 2) Webapp: uvicorn + a2wsgi 不支持 WebSocket，客户端只使用 polling，
#    否则浏览器会不断尝试 upgrade 并收到 403，页面显示 reconnecting。
js_files = sorted((site / "web/static/dist/assets").glob("index-*.js"))
if not js_files:
    raise SystemExit("no webapp bundle found")
for js in js_files:
    s = js.read_text()
    old = 'transports:["polling","websocket"]'
    new = 'transports:["polling"]'
    if new in s and old not in s:
        print("already patched:", js)
    elif old in s:
        js.write_text(s.replace(old, new))
        print("patched:", js)
    else:
        raise SystemExit(f"transports pattern not found in {js}")

# 3) 同步修改前端源码（如果包里带了 client 源码）
ts = site / "web/client/src/socket.ts"
if ts.is_file():
    s = ts.read_text()
    old = "transports: ['polling', 'websocket']"
    new = "transports: ['polling']"
    if new in s and old not in s:
        print("already patched:", ts)
    elif old in s:
        ts.write_text(s.replace(old, new))
        print("patched:", ts)
    else:
        print("warning: socket.ts pattern not found, skipped")

# 4) Webapp: 不要用缓存里的 MAC 自动 reconnect（S2 没有 link key，会卡在 reconnecting）
app_py = site / "web/app.py"
s = app_py.read_text()
old = """        reconnect_addresses = nuxbt.get_switch_addresses()
        index = nuxbt.create_controller(PRO_CONTROLLER, reconnect_address=reconnect_addresses)"""
new = """        # Switch 2 不保存 link key，带 reconnect_address 会一直卡在 reconnecting；
        # 这里始终等待一次全新的配对（Switch 1 也仍可正常使用）。
        index = nuxbt.create_controller(PRO_CONTROLLER)"""
if new in s and old not in s:
    print("already patched:", app_py)
elif old in s:
    app_py.write_text(s.replace(old, new))
    print("patched:", app_py)
else:
    raise SystemExit(f"reconnect pattern not found in {app_py}")

# 5) 模板加 query string，强制浏览器重新加载打过补丁的 JS
tmpl = site / "web/templates/index.html"
s = tmpl.read_text()
s2 = re.sub(r'(/static/dist/assets/index-[^\"]+\.js)(?:\?[^\"]*)?', r'\1?v=polling', s)
if s2 != s:
    tmpl.write_text(s2)
    print("patched (cache-bust):", tmpl)
else:
    print("already patched (cache-bust):", tmpl)

# 6) 控制器不要 180 秒后停止 discoverable；S2 配对可能等很久
ctrl = site / "controller/controller.py"
s = ctrl.read_text()
old = "        self.bt.set_discoverable_timeout(180)"
new = "        self.bt.set_discoverable_timeout(0)"
if new in s and old not in s:
    print("already patched:", ctrl)
elif old in s:
    ctrl.write_text(s.replace(old, new))
    print("patched:", ctrl)
else:
    raise SystemExit(f"discoverable timeout pattern not found in {ctrl}")
PY
