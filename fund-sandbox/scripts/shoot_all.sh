#!/usr/bin/env bash
# shoot_all.sh — 起一个带 CDP 调试端口的无头 Chrome，然后调用 cdp_shoot.cjs 给所有页面截图。
#
# 关键点：
#   1) 必须在不限制子进程的环境运行，否则 Chrome 的子进程会被 SIGTERM。
#   2) 必须用 --in-process-gpu + SwiftShader 软件渲染；用 --single-process 会让合成器崩（exit 3），
#      不用软件渲染则 GPU 进程起不来。
#   3) 不能用 --virtual-time-budget 截图：前端有 15s setInterval 自动刷新，虚拟时间会快进成死循环。
#
# 用法：bash scripts/shoot_all.sh [--views overview,positions,...] [--width 1440]
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHROME="${CHROME:-chrome}"
NODE="${NODE:-node}"
PORT="${CDP_PORT:-9222}"
PROFILE="${SFL_CHROME_PROFILE:-${TMPDIR:-/tmp}/sfl-chrome-profile}"
APP="${SFL_APP:-http://127.0.0.1:8765/}"

FLAGS=(--headless=new --no-sandbox --disable-dev-shm-usage --disable-gpu
        --in-process-gpu --use-gl=angle --use-angle=swiftshader
        --enable-unsafe-swiftshader --hide-scrollbars)

rm -rf "$PROFILE"; mkdir -p "$PROFILE"

"$CHROME" "${FLAGS[@]}" --remote-debugging-port="$PORT" --user-data-dir="$PROFILE" about:blank >/dev/null 2>&1 &
CPID=$!

for _ in $(seq 1 40); do
  if curl -s "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; then break; fi
  sleep 0.5
done
if ! curl -s "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; then
  echo "✗ Chrome 调试端口 $PORT 未就绪，退出"; kill "$CPID" 2>/dev/null; exit 1
fi

"$NODE" "$ROOT/scripts/cdp_shoot.cjs" --cdp "http://127.0.0.1:$PORT" --app "$APP" \
  --out "$ROOT/screenshots" "$@"
RC=$?

wait "$CPID" 2>/dev/null
exit $RC
