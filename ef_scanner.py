#!/usr/bin/env python3
"""
Jarvis EF route scanner — diffs /v1/internal/* routes in ef-sparse against
capabilities.json, posts new/removed routes to Slack for Chaplin to review.

Run daily via cron. Output goes to Slack DM if new routes found, silent otherwise.
"""
import json
import os
import re
import subprocess
import sys
import urllib.request

EF_PATH = os.path.expanduser("~/ef-sparse")
CAPABILITIES_PATH = os.path.join(os.path.dirname(__file__), "capabilities.json")
SLACK_CHANNEL = os.environ.get("JARVIS_LOG_CHANNEL", "D0BDUSZBB7V")  # DM to Chaplin
SLACK_TOKEN_ENV = "JARVIS_BOT_TOKEN"


def get_slack_token() -> str:
    """Extract JARVIS_BOT_TOKEN from environment or /proc/1/environ."""
    token = os.environ.get(SLACK_TOKEN_ENV, "")
    if token:
        return token
    try:
        with open("/proc/1/environ", "rb") as f:
            for kv in f.read().split(b"\x00"):
                if b"=" in kv:
                    k, v = kv.split(b"=", 1)
                    if k.decode() == SLACK_TOKEN_ENV:
                        return v.decode()
    except Exception:
        pass
    return ""


def scan_ef_routes(ef_path: str) -> set[str]:
    """Grep ef-sparse for all /v1/internal/* route strings."""
    try:
        result = subprocess.run(
            ["grep", "-rn", "--include=*.py", r"/v1/internal/", ef_path],
            capture_output=True, text=True, timeout=30,
        )
        routes: set[str] = set()
        for line in result.stdout.splitlines():
            # Extract quoted route strings
            for m in re.finditer(r'"(/v1/internal/[^"]+)"', line):
                route = m.group(1)
                # Skip test files
                if "/test" not in line.split(":")[0]:
                    routes.add(route)
        return routes
    except Exception as e:
        print(f"[scanner] grep failed: {e}", file=sys.stderr)
        return set()


def load_capability_endpoints(cap_path: str) -> set[str]:
    """Load all cms_endpoints currently tracked in capabilities.json."""
    try:
        with open(cap_path) as f:
            data = json.load(f)
        endpoints: set[str] = set()
        for op in data.get("ops", []):
            for ep in op.get("cms_endpoints", []):
                endpoints.add(ep)
        return endpoints
    except Exception as e:
        print(f"[scanner] failed to load capabilities.json: {e}", file=sys.stderr)
        return set()


def post_to_slack(token: str, channel: str, text: str) -> None:
    payload = json.dumps({"channel": channel, "text": text}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=10)


def main() -> None:
    ef_routes = scan_ef_routes(EF_PATH)
    tracked = load_capability_endpoints(CAPABILITIES_PATH)

    # New routes in EF not yet tracked in capabilities.json
    new_routes = sorted(ef_routes - tracked)
    # Tracked routes no longer found in EF
    removed_routes = sorted(tracked - ef_routes)

    if not new_routes and not removed_routes:
        print("[scanner] No new or removed routes — nothing to report.")
        return

    lines = ["*🔍 Jarvis EF Route Scan — Daily Diff*"]
    if new_routes:
        lines.append(f"\n*🆕 New /v1/internal/ routes in EF not yet in Jarvis capabilities ({len(new_routes)}):*")
        for r in new_routes:
            lines.append(f"  • `{r}`")
        lines.append("\n_Review and add to `capabilities.json` + implement if safe to expose._")
    if removed_routes:
        lines.append(f"\n*🗑 Routes in capabilities.json no longer found in EF ({len(removed_routes)}):*")
        for r in removed_routes:
            lines.append(f"  • `{r}`")
        lines.append("\n_These may have been renamed or removed upstream — check before deprecating._")

    message = "\n".join(lines)
    print(message)

    token = get_slack_token()
    if token:
        try:
            post_to_slack(token, SLACK_CHANNEL, message)
            print("[scanner] Posted to Slack.")
        except Exception as e:
            print(f"[scanner] Slack post failed: {e}", file=sys.stderr)
    else:
        print("[scanner] No Slack token — printed only.", file=sys.stderr)


if __name__ == "__main__":
    main()
