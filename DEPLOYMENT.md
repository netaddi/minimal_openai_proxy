# Deployment Guide

This guide describes how to deploy and operate `minimal_openai_proxy` on a
generic Linux server. It intentionally avoids machine-specific hostnames, IP
addresses, personal paths, and private credentials.

## Overview

The proxy forwards OpenAI-compatible HTTP requests to an upstream endpoint and
rewrites the top-level request `model` field through `model_map`.

Default shared model map:

```text
gpt-5.5 -> gpt-5.5-0424-global
gpt-5.4 -> gpt-5.4-0305-global
gpt-5.3 -> gpt-5.3-chat-0303-global
```

API keys are not stored by the proxy. Each client must send its own
`Authorization` header.

## Current Deployments

Current internal deployments use the same repository layout, the same local
`config.json` contents, the same `0.0.0.0:18080` listener, and the same
`nohup`/`proxy.pid` process model. Machine-local files such as `config.json`,
`proxy.log`, `usage.jsonl`, and `proxy.pid` remain untracked.

```text
host: 3090x8
repo: /home/admin/wangyin.yx/workspace/minimal_openai_proxy
runtime: python3
listen: 0.0.0.0:18080
direct base URL: http://11.166.42.141:18080/v1
health check: http://11.166.42.141:18080/healthz
```

```text
host: l20-new
repo: /home/wangyin.yx/workspace/minimal_openai_proxy
runtime: /home/wangyin.yx/.local/share/uv/python/cpython-3.11-linux-x86_64-gnu/bin/python3.11
listen: 0.0.0.0:18080
direct base URL: http://11.139.21.94:18080/v1
health check: http://11.139.21.94:18080/healthz
```

`l20-new` is normally reached through the `l20-new` SSH alias, which uses
`jump-ea120`. If direct HTTP to `11.139.21.94:18080` is blocked from the current
workstation network, use an SSH tunnel:

```bash
ssh -L 18080:127.0.0.1:18080 l20-new
export OPENAI_BASE_URL="http://127.0.0.1:18080/v1"
```

## Prerequisites

- Linux server with Python 3.9+.
- Network access from the server to the upstream OpenAI-compatible endpoint.
- Network access from clients to the proxy, or SSH access for tunneling.
- Git access to this repository.

## Clone

```bash
mkdir -p ~/workspace
cd ~/workspace
git clone git@gitlab.alibaba-inc.com:wangyin.yx/minimal_openai_proxy.git
cd minimal_openai_proxy
```

## Configure

Create a local config:

```bash
cp config.example.json config.json
```

Edit `config.json` and set `target_base_url` for your upstream endpoint:

```json
{
  "host": "0.0.0.0",
  "port": 18080,
  "target_base_url": "https://your-enterprise-openai.example.com/v1",
  "strip_prefix": "auto",
  "usage_log_path": "usage.jsonl",
  "usage_capture_max_bytes": 1048576,
  "model_map": {
    "gpt-5.5": "gpt-5.5-0424-global",
    "gpt-5.4": "gpt-5.4-0305-global",
    "gpt-5.3": "gpt-5.3-chat-0303-global"
  }
}
```

Do not put API keys in `config.json`.

## Start

Foreground start for quick verification:

```bash
python3 minimal_openai_proxy.py --host 0.0.0.0 --port 18080
```

Background start with `nohup`:

```bash
nohup python3 minimal_openai_proxy.py --host 0.0.0.0 --port 18080 > proxy.log 2>&1 &
echo $! > proxy.pid
```

Use `--host 127.0.0.1` instead of `0.0.0.0` if clients should only access the
proxy through SSH tunneling or a local reverse proxy.

## Client Configuration

If clients can directly reach the server:

```bash
export OPENAI_BASE_URL="http://your-server:18080/v1"
export OPENAI_API_KEY="<enterprise key>"
```

Smoke test:

```bash
curl "$OPENAI_BASE_URL/responses" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.5","input":"Say OK in one word.","max_output_tokens":64}'
```

If direct access is blocked, keep an SSH tunnel open from the client machine:

```bash
ssh -L 18080:127.0.0.1:18080 your-server
```

Then configure clients with:

```bash
export OPENAI_BASE_URL="http://127.0.0.1:18080/v1"
export OPENAI_API_KEY="<enterprise key>"
```

## Check Status

```bash
cat proxy.pid
ps -fp "$(cat proxy.pid)"
curl -sS http://127.0.0.1:18080/healthz
tail -f proxy.log
tail -f usage.jsonl
```

Expected health response:

```json
{"ok":true}
```

Current host checks:

```bash
ssh 3090x8 'cd ~/workspace/minimal_openai_proxy && git status --short --branch && ps -fp "$(cat proxy.pid)" && curl -sS http://127.0.0.1:18080/healthz'
ssh l20-new 'cd ~/workspace/minimal_openai_proxy && git status --short --branch && ps -fp "$(cat proxy.pid)" && curl -sS http://127.0.0.1:18080/healthz'
```

## Stop

```bash
kill "$(cat proxy.pid)" 2>/dev/null || true
rm -f proxy.pid
```

## Restart

```bash
kill "$(cat proxy.pid)" 2>/dev/null || true
rm -f proxy.pid
nohup python3 minimal_openai_proxy.py --host 0.0.0.0 --port 18080 > proxy.log 2>&1 &
echo $! > proxy.pid
```

For `l20-new`, use its uv-managed Python runtime:

```bash
ssh l20-new '
cd ~/workspace/minimal_openai_proxy
PYTHON=/home/wangyin.yx/.local/share/uv/python/cpython-3.11-linux-x86_64-gnu/bin/python3.11
kill "$(cat proxy.pid)" 2>/dev/null || true
rm -f proxy.pid
nohup "$PYTHON" minimal_openai_proxy.py --host 0.0.0.0 --port 18080 </dev/null > proxy.log 2>&1 &
echo $! > proxy.pid
'
```

## Update

Use the current checked-out branch for updates, normally `main`. Do not create
new branches; commit changes to the current branch and push that branch.

```bash
git pull --ff-only
python3 -m unittest discover -s tests
kill "$(cat proxy.pid)" 2>/dev/null || true
rm -f proxy.pid
nohup python3 minimal_openai_proxy.py --host 0.0.0.0 --port 18080 > proxy.log 2>&1 &
echo $! > proxy.pid
```

For `l20-new`, replace `python3` with the uv-managed runtime path shown in
`Current Deployments`.

## Logs And Cost

Process log:

```bash
tail -f proxy.log
```

Usage log:

```bash
tail -f usage.jsonl
```

Cost calculation uses a local pricing file:

```bash
cp pricing.example.json pricing.json
# edit pricing.json with real rates
python3 scripts/calculate_cost.py --pricing pricing.json usage.jsonl
```

## Troubleshooting

- `404` or upstream path errors: check `target_base_url` and `strip_prefix`.
- `401` or `403`: check the client-sent `Authorization` header.
- Streaming hangs or disconnects: inspect `proxy.log` and retry a small
  non-streaming `/v1/responses` request.
- Empty usage fields: check `usage_parse_warning` in `usage.jsonl`.
- Wrong upstream model: check `config.json` or `OPENAI_PROXY_MODEL_MAP`.
- Port already in use: choose another `--port` or stop the existing listener.

## Security Notes

- Do not commit `config.json`, `pricing.json`, `proxy.log`, `usage.jsonl`, or
  `proxy.pid`.
- Do not put API keys in config files, command examples, logs, or commits.
- The proxy does not authenticate clients by itself. If binding to `0.0.0.0`,
  protect the service with network controls, firewall rules, SSH tunneling, or a
  trusted reverse proxy.
