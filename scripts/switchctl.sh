#!/usr/bin/env bash
# switchctl.sh - Switch 2 / nuxbt 调试与连接管理
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

NUXBT="$ROOT/.venv/bin/nuxbt"
PY="$ROOT/.venv/bin/python"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"

usage() {
  cat <<'EOF'
switchctl.sh - Switch 2 / nuxbt 调试工具

用法:
  ./scripts/switchctl.sh <命令> [参数]

命令:
  status                查看适配器、配对、进程、最近连接状态
  enable                启用 nuxbt 的 BlueZ override（需要 sudo；重启后需重新执行）
  disable               回滚 override（需要 sudo）
  check                 仅检查 nuxbt plugin 状态
  addresses             列出 BlueZ 中记录的 Switch
  test [参数]           连接测试：等待配对，然后每 2 秒按方向键
                        默认 120 秒；参数透传给 test_link.py
                        例: ./scripts/switchctl.sh test
                            ./scripts/switchctl.sh test --seconds 30 --button A
  pair                  同 test
  reset                 清理残留进程 + 重启蓝牙 + 拉起 hci0（连不上时先试这个）
  newmac [MAC]          换一个新的蓝牙 MAC（默认随机 Nintendo OUI），解决认证失败
  reconnect [MAC]       尝试用已保存的配对重连（S2 大概率失败，仅用于调试）
  macro <文件|字符串> [额外参数]
                        运行宏，额外参数透传给 nuxbt macro
                        例: ./scripts/switchctl.sh macro macros/demo.txt
                            ./scripts/switchctl.sh macro macros/demo.txt -a 78:81:8C:16:59:B7
  btmon [秒数]          抓取 HCI 日志，默认 20 秒
  logs [文件]           跟踪日志，默认最新 .out
  stop                  停止正在运行的 test_link / nuxbt
  webapp [参数]         启动 Web 控制页面（默认 0.0.0.0:8000）
                        例: ./scripts/switchctl.sh webapp
                            ./scripts/switchctl.sh webapp -i 127.0.0.1 -p 8080
  install               把 clone 中的 nuxbt 以 editable 模式装入 venv
                        （会重启 webapp 才能生效）
  env                   打印版本与环境
  demo                  运行官方 demo（S2 上有风险，需输入 yes 确认）
  help                  显示帮助

典型流程:
  1. 重启 Pi 后:  ./scripts/switchctl.sh enable
  2. Switch 2 进 Controllers -> Change Grip/Order
  3. 运行:        ./scripts/switchctl.sh test
  4. 连上后按一次 A 退出菜单，观察 120 秒是否掉线
EOF
}

require_venv() {
  if [ ! -x "$NUXBT" ] || [ ! -x "$PY" ]; then
    echo "错误：找不到 $NUXBT，请先创建 venv 并安装 nuxbt。" >&2
    exit 1
  fi
}

nuxbt_running() {
  pgrep -f '[n]uxbt webapp|[t]est_link.py' >/dev/null 2>&1
}

random_nintendo_mac() {
  printf '7C:BB:8A:%02X:%02X:%02X' \
    $((RANDOM % 256)) $((RANDOM % 256)) $((RANDOM % 256))
}

apply_bt_mac() {
  local mac="$1"
  local b1 b2 b3 b4 b5 b6

  if ! [[ "$mac" =~ ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$ ]]; then
    echo "无效 MAC: $mac" >&2
    return 1
  fi

  IFS=: read -r b1 b2 b3 b4 b5 b6 <<< "$mac"
  echo "== 设置蓝牙 MAC: $mac =="

  # 先确保适配器是 up 的，Broadcom 的 Write_BD_ADDR 命令才能成功
  sudo hciconfig hci0 up 2>/dev/null || true
  sudo hcitool -i hci0 cmd 0x3f 0x001 \
    "0x$b6" "0x$b5" "0x$b4" "0x$b3" "0x$b2" "0x$b1"
  sudo hciconfig hci0 down
  sudo hciconfig hci0 up
  sudo systemctl restart bluetooth
  sleep 1
  sudo hciconfig hci0 up
  sleep 1

  hciconfig hci0 | head -3
  bluetoothctl show 2>/dev/null | grep -E "Controller|Powered" || true
}

enable_plugin() {
  local py_real
  py_real="$(readlink -f "$PY")"

  if nuxbt_running; then
    echo "检测到 nuxbt webapp/test_link 正在运行，请先执行 ./scripts/switchctl.sh stop" >&2
    return 1
  fi

  echo "== 启用 nuxbt BlueZ override =="
  sudo mkdir -p /run/systemd/system/bluetooth.service.d
  sudo tee /run/systemd/system/bluetooth.service.d/nuxbt.conf >/dev/null <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/libexec/bluetooth/bluetoothd --compat --noplugin=*
EOF
  sudo setcap 'cap_net_raw,cap_net_admin,cap_net_bind_service+eip' "$py_real"
  sudo systemctl daemon-reload

  # Switch 2 会保存 link key，而 Pi 侧不保存（Bonded: no）。
  # 每次 enable 默认换一个新 MAC，保证 S2 走全新配对，避免 Authentication Failure。
  local mac="${NUXBT_BT_MAC:-$(random_nintendo_mac)}"
  apply_bt_mac "$mac"

  echo "== 结果 =="
  "$NUXBT" check
  hciconfig hci0 2>&1 | head -4
}

newmac() {
  local mac="${1:-$(random_nintendo_mac)}"

  if nuxbt_running; then
    echo "检测到 nuxbt webapp/test_link 正在运行，请先执行 ./scripts/switchctl.sh stop" >&2
    return 1
  fi

  apply_bt_mac "$mac"
  echo ""
  echo "新 MAC: $mac"
  echo "接下来启动 webapp 并在 Switch 2 的 Change Grip/Order 重新配对："
  echo "  ./scripts/switchctl.sh webapp"
}

install_clone() {
  if [ ! -f "$ROOT/nuxbt/pyproject.toml" ]; then
    echo "找不到 $ROOT/nuxbt，请先 git clone 上游仓库。" >&2
    return 1
  fi
  if nuxbt_running; then
    echo "检测到 nuxbt 正在运行，请先执行 ./scripts/switchctl.sh stop" >&2
    echo "（editable 安装会替换当前包，重启 webapp 后才生效）" >&2
    return 1
  fi

  echo "== 应用本地 patch 到 clone =="
  "$ROOT/scripts/apply_patches.sh" "$ROOT/nuxbt/nuxbt"

  echo "== editable 安装 =="
  "$ROOT/.venv/bin/pip" install -e "$ROOT/nuxbt" --no-deps
  "$NUXBT" --version
  echo ""
  echo "完成。现在可以启动：./scripts/switchctl.sh webapp"
}


disable_plugin() {
  local py_real
  py_real="$(readlink -f "$PY")"

  echo "== 回滚 nuxbt BlueZ override =="
  sudo rm -f /run/systemd/system/bluetooth.service.d/nuxbt.conf
  sudo setcap -r "$py_real" 2>/dev/null || true
  sudo systemctl daemon-reload
  sudo systemctl restart bluetooth
  sudo hciconfig hci0 up 2>/dev/null || true
  "$NUXBT" check
}

ensure_ready() {
  if ! "$NUXBT" check 2>/dev/null | grep -q "Enabled"; then
    echo "错误：NUXBT override 未启用（Pi 重启后 /run 会被清空）。" >&2
    echo "请先运行: ./scripts/switchctl.sh enable" >&2
    return 1
  fi
  if ! hciconfig hci0 2>/dev/null | grep -q "UP RUNNING"; then
    echo "hci0 未 UP，正在拉起..." >&2
    sudo hciconfig hci0 up
  fi
}

show_status() {
  local mac=""
  echo "== nuxbt =="
  "$NUXBT" --version || true
  echo "== plugin =="
  "$NUXBT" check || true
  echo "== hci0 =="
  hciconfig hci0 2>&1 | head -5 || true
  echo "== adapter =="
  bluetoothctl show 2>/dev/null | grep -E "Powered|Discoverable|Pairable|Alias|Class" || true
  echo "== switches =="
  "$NUXBT" addresses 2>&1 || true
  if [ -f "$LOG_DIR/last_mac" ]; then
    mac="$(tr -d '[:space:]' < "$LOG_DIR/last_mac")"
  fi
  if [ -n "$mac" ]; then
    echo "== last connection ($mac) =="
    bluetoothctl info "$mac" 2>/dev/null | grep -E "Name|Paired|Bonded|Trusted|Connected" || true
  fi
  echo "== processes =="
  pgrep -af "[t]est_link.py|[.]venv/bin/nuxbt" || echo "(none)"
  if [ -e "$LOG_DIR/test.lock" ] && ! flock -n "$LOG_DIR/test.lock" true 2>/dev/null; then
    echo "== test lock =="
    echo "HELD（已有测试在运行）"
  fi
  echo "== latest logs =="
  ls -t "$LOG_DIR"/*.out 2>/dev/null | head -3 || true
}

run_test() {
  ensure_ready

  local lock="$LOG_DIR/test.lock"
  exec 9>"$lock"
  if ! flock -n 9; then
    echo "已有测试在运行（锁: $lock）。" >&2
    echo "请先执行: ./scripts/switchctl.sh stop  或  ./scripts/switchctl.sh reset" >&2
    return 1
  fi

  echo "等待 Switch 2 连接：请进入 Controllers -> Change Grip/Order"
  "$PY" "$ROOT/scripts/test_link.py" --seconds 120 --timeout 600 "$@" &
  local child=$!
  trap 'kill -TERM "$child" 2>/dev/null || true' INT TERM

  local rc=0
  if wait "$child"; then rc=0; else rc=$?; fi
  trap - INT TERM

  if [ "$rc" -eq 2 ]; then
    echo "未在超时时间内连接。请确认 Switch 2 在 Change Grip/Order，或先执行 reset。" >&2
  fi
  return "$rc"
}

run_reconnect() {
  ensure_ready
  local mac="${1:-}"
  if [ $# -gt 0 ]; then shift; fi
  local macro="${1:-$'DPAD_RIGHT 0.1s\n0.5s'}"

  echo "注意：Switch 2 通常不保存 link key，重连很可能超时。"
  echo "超时后请用 ./scripts/switchctl.sh test 重新配对。"
  if [ -n "$mac" ]; then
    timeout "${RECONNECT_TIMEOUT:-45}" "$NUXBT" macro -a "$mac" -c "$macro" || \
      echo "重连失败/超时（exit=$?）"
  else
    timeout "${RECONNECT_TIMEOUT:-45}" "$NUXBT" macro -r -c "$macro" || \
      echo "重连失败/超时（exit=$?）"
  fi
}

run_macro() {
  if [ $# -lt 1 ]; then
    echo "用法: $0 macro <文件|字符串> [nuxbt macro 参数]" >&2
    return 1
  fi
  ensure_ready
  local src="$1"
  shift
  "$NUXBT" macro -c "$src" "$@"
}

run_btmon() {
  local secs="${1:-20}"
  local out="$LOG_DIR/btmon-$(date +%Y%m%d-%H%M%S).log"
  echo "抓取 ${secs}s HCI 日志 -> $out"
  sudo timeout "$secs" btmon >"$out" 2>&1 || true
  echo "完成。关键事件："
  grep -E "Inquiry|Connect|Disconnect|Link Key|PIN|Authentication|Request" "$out" | tail -30 || true
}

follow_logs() {
  local f="${1:-}"
  if [ -z "$f" ]; then
    f="$(ls -t "$LOG_DIR"/*.out 2>/dev/null | head -1 || true)"
  elif [ ! -f "$f" ] && [ -f "$LOG_DIR/$f" ]; then
    f="$LOG_DIR/$f"
  fi
  if [ -z "$f" ] || [ ! -f "$f" ]; then
    echo "没有找到日志文件。" >&2
    return 1
  fi
  echo "跟踪 $f（Ctrl-C 退出）"
  tail -n 50 -f "$f"
}

stop_all() {
  local pids self=$$ parent=$PPID
  pids="$(pgrep -f '[t]est_link.py|[.]venv/bin/nuxbt' 2>/dev/null | grep -vw -e "$self" -e "$parent" || true)"
  if [ -n "$pids" ]; then
    echo "发送 SIGINT: $pids"
    kill -INT $pids 2>/dev/null || true
    sleep 2
    pids="$(pgrep -f '[t]est_link.py|[.]venv/bin/nuxbt' 2>/dev/null | grep -vw -e "$self" -e "$parent" || true)"
    if [ -n "$pids" ]; then
      # 子进程可能阻塞在 recv()，SIGTERM 会触发 nuxbt 的 SystemExit 并打印 traceback；
      # 这里直接用 SIGKILL，避免污染日志。
      echo "仍在运行，发送 SIGKILL: $pids"
      kill -KILL $pids 2>/dev/null || true
      sleep 1
    fi
  fi
  pgrep -af '[t]est_link.py|[.]venv/bin/nuxbt' 2>/dev/null | grep -vw -e "$self" -e "$parent" || echo "已停止（无残留进程）"
}

reset_all() {
  stop_all
  rm -f "$LOG_DIR/test.lock"
  echo "== 重启 bluetooth =="
  sudo systemctl restart bluetooth
  sudo hciconfig hci0 up
  sleep 1
  show_status
}

run_demo() {
  cat <<'EOF'
警告：nuxbt demo 是 Switch 1 的盲导航宏，在 Switch 2 上可能误触 Sleep/退出菜单。
建议改用: ./scripts/switchctl.sh test
EOF
  read -r -p "仍要继续? 输入 yes: " ans
  if [ "$ans" != "yes" ]; then
    echo "已取消。"
    return 1
  fi
  ensure_ready
  "$NUXBT" demo
}

run_webapp() {
  ensure_ready
  if pgrep -f '[t]est_link.py' >/dev/null 2>&1; then
    echo "检测到 test_link 正在运行。webapp 和它不能同时占用适配器。" >&2
    echo "请先执行: ./scripts/switchctl.sh stop" >&2
    return 1
  fi

  local ip="0.0.0.0" port="8000"
  local -a rest=()
  while [ $# -gt 0 ]; do
    case "$1" in
      -i|--ip)   ip="${2:-0.0.0.0}"; shift 2 ;;
      -p|--port) port="${2:-8000}"; shift 2 ;;
      *)         rest+=("$1"); shift ;;
    esac
  done

  local lan_ip=""
  lan_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  local bt_mac=""
  bt_mac="$(hciconfig hci0 2>/dev/null | awk '/BD Address/{print $3}')"
  echo "== NUXBT Webapp =="
  echo "  蓝牙 MAC: ${bt_mac:-unknown}"
  echo "  本机:   http://127.0.0.1:${port}"
  if [ -n "$lan_ip" ]; then
    echo "  局域网: http://${lan_ip}:${port}"
    echo "  SSH 隧道: ssh -L ${port}:127.0.0.1:${port} ${USER}@${lan_ip}"
  fi
  echo "  警告: 页面无鉴权，不要暴露到公网；同一适配器只能创建一个控制器。"
  echo "  提示: 浏览器 Gamepad API 需要 HTTPS 或 localhost；局域网 HTTP 下用键盘/宏即可。"

  "$NUXBT" webapp -i "$ip" -p "$port" "${rest[@]}"
}

show_env() {
  "$NUXBT" --version
  "$PY" --version
  uname -a
  tr -d '\0' < /proc/device-tree/model 2>/dev/null || true
  echo
  hciconfig hci0 2>&1 | head -4 || true
}

cmd="${1:-help}"
if [ $# -gt 0 ]; then shift; fi

case "$cmd" in
  help|-h|--help) usage ;;
  status)         require_venv; show_status ;;
  enable)         require_venv; enable_plugin ;;
  disable)        require_venv; disable_plugin ;;
  check)          require_venv; "$NUXBT" check ;;
  addresses)      require_venv; "$NUXBT" addresses ;;
  test|pair)      require_venv; run_test "$@" ;;
  reset)          require_venv; reset_all ;;
  newmac)         require_venv; newmac "$@" ;;
  reconnect)      require_venv; run_reconnect "$@" ;;
  macro)          require_venv; run_macro "$@" ;;
  btmon)          require_venv; run_btmon "$@" ;;
  logs)           require_venv; follow_logs "$@" ;;
  stop)           require_venv; stop_all ;;
  webapp)         require_venv; run_webapp "$@" ;;
  install)        require_venv; install_clone ;;
  env)            require_venv; show_env ;;
  demo)           require_venv; run_demo ;;
  *)              usage; exit 1 ;;
esac
