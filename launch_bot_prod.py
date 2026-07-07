#!/usr/bin/env python3
"""Launch the Jarvis PROD bot on port 8089, pointing at prod CMS + prod Slack channels."""
import os, sys, subprocess

# Get Anthropic key from process 1 environment
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

def secret(name):
    r = subprocess.run(
        [sys.executable, '/opt/genesis/manage-secrets.py', 'get', name],
        capture_output=True, text=True
    )
    v = r.stdout.strip()
    return v if v and not v.startswith('no such secret') else None

print(f"Launching PROD bot with key: {key[:12]}...")

env = os.environ.copy()
env['ANTHROPIC_API_KEY'] = key

# --- PROD env markers ---
env['JARVIS_ENV'] = 'prod'
env['JARVIS_BOT_PORT'] = '8089'  # distinct port from dev (8088)

# Prod Slack tokens
bot_tok = secret('SLACK_BOT_TOKEN_JARVIS_PROD')
app_tok = secret('SLACK_APP_TOKEN_JARVIS_PROD')
if not bot_tok:
    print("ERROR: SLACK_BOT_TOKEN_JARVIS_PROD not found"); sys.exit(1)
if not app_tok:
    print("ERROR: SLACK_APP_TOKEN_JARVIS_PROD not found"); sys.exit(1)

env['SLACK_BOT_TOKEN'] = bot_tok
env['SLACK_APP_TOKEN'] = app_tok
print(f"Using prod bot token: {bot_tok[:16]}...")
print(f"Using prod app token: {app_tok[:16]}...")

# Prod channels
env['SLACK_HOME_CHANNEL'] = 'C0BFA0UQLB1'   # #bot-heygen-jarvis-prod
env['JARVIS_LOG_CHANNEL'] = 'C0BFV571RRS'    # #jarvis-log-prod

# Prod bot user ID
env['JARVIS_BOT_USER_ID'] = 'U0BGKMSC9L0'   # @jarvisprod

# Python path
user_site = subprocess.run(
    [sys.executable, '-c', 'import site; print(site.getusersitepackages())'],
    capture_output=True, text=True
).stdout.strip()
if user_site:
    existing = env.get('PYTHONPATH', '')
    env['PYTHONPATH'] = f"{user_site}:{existing}" if existing else user_site

os.chdir('/home/hermes/jarvis-prototype')
os.execve(sys.executable, [sys.executable, '/home/hermes/jarvis-prototype/bot.py'], env)
