# Deployment Guide

This document describes the current `minimal_openai_proxy` deployment on
`3090x8` and how to operate it from a workstation such as a macOS office laptop.

## Current Deployment

```text
host: 3090x8
repo: /home/admin/wangyin.yx/workspace/minimal_openai_proxy
listen: 0.0.0.0:18080
public base URL: http://3090x8:18080/v1
local server URL: http://127.0.0.1:18080/v1
health check: http://3090x8:18080/healthz
usage log: /home/admin/wangyin.yx/workspace/minimal_openai_proxy/usage.jsonl
process log: /home/admin/wangyin.yx/workspace/minimal_openai_proxy/proxy.log
pid file: /home/admin/wangyin.yx/workspace/minimal_openai_proxy/proxy.pid
```

The proxy is configured to forward to the enterprise OpenAI-compatible endpoint
and rewrite public OpenAI model names to enterprise model names. API keys are not
stored in the proxy config; clients must send their own `Authorization` header.

Current required mappings:

```text
gpt-5.5 -> gpt-5.5-0424-global
gpt-5.4 -> gpt-5.4-0305-global
gpt-5.3 -> gpt-5.3-chat-0303-global
```

## Workstation Access

### Direct Access

Because the service binds `0.0.0.0:18080`, a workstation that can resolve and
reach `3090x8` can use:

```bash
export OPENAI_BASE_URL="http://3090x8:18080/v1"
export OPENAI_API_KEY="<enterprise key>"
```

Smoke test:

```bash
curl http://3090x8:18080/v1/responses \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.5","input":"Say OK in one word.","max_output_tokens":64}'
```

### SSH Tunnel Fallback

If direct access to `3090x8:18080` is blocked from the office network, keep an
SSH tunnel open:

```bash
ssh -L 18080:127.0.0.1:18080 3090x8
```

Then use:

```bash
export OPENAI_BASE_URL="http://127.0.0.1:18080/v1"
export OPENAI_API_KEY="<enterprise key>"
```

## Codex CLI From macOS

For Codex CLI on a workstation, configure the built-in OpenAI provider to use
the proxy by setting `openai_base_url` in `~/.codex/config.toml`:

```toml
openai_base_url = "http://3090x8:18080/v1"
```

Keep the API key in the existing Codex/OpenAI credential path or export it in
the shell before running Codex:

```bash
export OPENAI_API_KEY="<enterprise key>"
codex exec "Say OK in one word."
```

If using an SSH tunnel instead, set:

```toml
openai_base_url = "http://127.0.0.1:18080/v1"
```

## Operations On 3090x8

### Check Status

```bash
ssh 3090x8 'cd ~/workspace/minimal_openai_proxy && git status --short --branch'
ssh 3090x8 'ps -fp "$(cat ~/workspace/minimal_openai_proxy/proxy.pid)"'
ssh 3090x8 'ss -ltn | grep :18080'
ssh 3090x8 'curl -sS http://127.0.0.1:18080/healthz'
```

Expected health response:

```json
{"ok":true}
```

### Start

```bash
ssh 3090x8 '
cd ~/workspace/minimal_openai_proxy
nohup python3 minimal_openai_proxy.py --host 0.0.0.0 --port 18080 </dev/null > proxy.log 2>&1 &
echo $! > proxy.pid
'
```

### Stop

```bash
ssh 3090x8 '
cd ~/workspace/minimal_openai_proxy
kill "$(cat proxy.pid)" 2>/dev/null || true
rm -f proxy.pid
'
```

### Restart

```bash
ssh 3090x8 '
cd ~/workspace/minimal_openai_proxy
kill "$(cat proxy.pid)" 2>/dev/null || true
rm -f proxy.pid
nohup python3 minimal_openai_proxy.py --host 0.0.0.0 --port 18080 </dev/null > proxy.log 2>&1 &
echo $! > proxy.pid
'
```

### Update Code

```bash
ssh 3090x8 '
cd ~/workspace/minimal_openai_proxy
git pull --ff-only
python3 -m unittest discover -s tests
kill "$(cat proxy.pid)" 2>/dev/null || true
rm -f proxy.pid
nohup python3 minimal_openai_proxy.py --host 0.0.0.0 --port 18080 </dev/null > proxy.log 2>&1 &
echo $! > proxy.pid
'
```

## Logs And Cost

Process log:

```bash
ssh 3090x8 'tail -f ~/workspace/minimal_openai_proxy/proxy.log'
```

Usage log:

```bash
ssh 3090x8 'tail -f ~/workspace/minimal_openai_proxy/usage.jsonl'
```

Cost calculation uses a local pricing file. Copy the example and fill in real
enterprise prices:

```bash
ssh 3090x8 '
cd ~/workspace/minimal_openai_proxy
cp pricing.example.json pricing.json
# edit pricing.json with real prices
python3 scripts/calculate_cost.py --pricing pricing.json usage.jsonl
'
```

## Notes

- The proxy does not authenticate clients by itself. Since the current deployment
  binds all interfaces, ensure `3090x8:18080` is reachable only from trusted
  networks or protected by network controls.
- The proxy does not impose a request body size limit. Upstream endpoint limits
  are the source of truth.
- Chunked request bodies are not supported and return `501`; normal JSON
  requests with `Content-Length` are supported. Codex CLI and standard OpenAI
  SDK calls normally send JSON requests with `Content-Length`.
