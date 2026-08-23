#!/bin/sh
set -eu

if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
    echo "LLDB capture module unavailable" >&2
    exit 2
fi
capture_module=$1
lldb_path=$(/usr/bin/xcrun --find lldb)
attempt=0
while [ "$attempt" -lt 1800 ]; do
    wechat_pid=$(/usr/bin/pgrep -x WeChat | /usr/bin/head -n 1 || true)
    if [ -n "$wechat_pid" ]; then
        echo "WECHAT_PID $wechat_pid"
        "$lldb_path" \
            -p "$wechat_pid" -b \
            -o "command script import \"$capture_module\"" \
            -o wechat-key-install -o continue -o wechat-key-emit -o detach -o quit &
        debugger_pid=$!
        (
            /bin/sleep 120
            /bin/kill -KILL "$debugger_pid" 2>/dev/null || true
            /bin/kill -CONT "$wechat_pid" 2>/dev/null || true
        ) >/dev/null 2>&1 &
        watchdog_pid=$!
        status=0
        wait "$debugger_pid" || status=$?
        /bin/kill "$watchdog_pid" 2>/dev/null || true
        /bin/kill -CONT "$wechat_pid" 2>/dev/null || true
        exit "$status"
    fi
    /bin/sleep 0.1
    attempt=$((attempt + 1))
done

echo "WeChat process did not start in time" >&2
exit 2
