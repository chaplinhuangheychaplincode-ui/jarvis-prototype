#!/usr/bin/env python3
"""Launch the Jarvis bot, extracting the API key from process 1 environment."""
import os, sys, subprocess

# Get key from /proc/1/environ
key = ""
with open('/proc/1/environ', 'rb') as f:
    for item in f.read().split(b'\x00'):
        decoded = item.decode('utf-8', errors='ignore')
        if decoded.startswith('ANTHROPIC_API_KEY='):
            key = decoded.split('=', 1)[1]
            break

if not key:
    print("ERROR: ANTHROPIC_API_KEY not found in /proc/1/environ")
    sys.exit(1)

print(f"Launching bot with key: {key[:12]}...")

env = os.environ.copy()
env['ANTHROPIC_API_KEY'] = key
env['SLACK_HOME_CHANNEL'] = 'C0BDT0WDDV5'
# Log channel for audit trail posting — set to empty string to disable
env['JARVIS_LOG_CHANNEL'] = 'C0BEW7TE50S'  # #jarvis-log-dev

# Use the dedicated Jarvis bot token (separate from the gateway token)
jarvis_tok = subprocess.run(
    [sys.executable, '/opt/genesis/manage-secrets.py', 'get', 'SLACK_BOT_TOKEN_JARVIS'],
    capture_output=True, text=True
).stdout.strip()
if jarvis_tok and not jarvis_tok.startswith('no such secret'):
    env['SLACK_BOT_TOKEN'] = jarvis_tok
    print(f"Using dedicated Jarvis bot token: {jarvis_tok[:16]}...")

# Use the dedicated Jarvis app-level token for Socket Mode
jarvis_app_tok = subprocess.run(
    [sys.executable, '/opt/genesis/manage-secrets.py', 'get', 'SLACK_APP_TOKEN_JARVIS'],
    capture_output=True, text=True
).stdout.strip()
if jarvis_app_tok and not jarvis_app_tok.startswith('no such secret'):
    env['SLACK_APP_TOKEN'] = jarvis_app_tok
    print(f"Using dedicated Jarvis app token: {jarvis_app_tok[:16]}...")
# Ensure flask is importable (installed to user site)
user_site = subprocess.run(
    [sys.executable, '-c', 'import site; print(site.getusersitepackages())'],
    capture_output=True, text=True
).stdout.strip()
if user_site:
    existing = env.get('PYTHONPATH', '')
    env['PYTHONPATH'] = f"{user_site}:{existing}" if existing else user_site

os.chdir('/home/hermes/jarvis-prototype')
os.execve(sys.executable, [sys.executable, '/home/hermes/jarvis-prototype/bot.py'], env)
