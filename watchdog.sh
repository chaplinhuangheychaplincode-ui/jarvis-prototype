#!/bin/bash
# Jarvis bot watchdog — restart if not running, notify Slack
LOG=/tmp/jarvis_bot.log
PIDFILE=/tmp/jarvis_bot.pid

is_running() {
    if [ -f "$PIDFILE" ]; then
        pid=$(cat "$PIDFILE")
        kill -0 "$pid" 2>/dev/null && return 0
    fi
    # fallback: check by process name
    pgrep -f "python3.*launch_bot.py" > /dev/null 2>&1
}

if is_running; then
    exit 0
fi

# Not running — restart
echo "[watchdog] $(date -u): bot not running, restarting..." >> "$LOG"
cd /home/hermes/jarvis-prototype
nohup python3 launch_bot.py >> "$LOG" 2>&1 &
echo $! > "$PIDFILE"

# Notify Slack via bot token
SLACK_TOKEN=$(python3 -c "
import subprocess, os
env = {}
with open('/proc/1/environ', 'rb') as f:
    for kv in f.read().split(b'\x00'):
        if b'=' in kv:
            k, v = kv.split(b'=', 1)
            env[k.decode()] = v.decode()
print(env.get('JARVIS_BOT_TOKEN', ''))
" 2>/dev/null)

if [ -n "$SLACK_TOKEN" ]; then
    curl -s -X POST "https://slack.com/api/chat.postMessage" \
        -H "Authorization: Bearer $SLACK_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"channel\":\"D0BDUSZBB7V\",\"text\":\"⚠️ Jarvis watchdog: bot was down, restarted at $(date -u)\"}" \
        > /dev/null
fi
