# minimal_openai_proxy

`minimal_openai_proxy` is a tiny OpenAI-compatible HTTP proxy for environments
where an enterprise OpenAI endpoint uses custom model names, while local tools
expect official OpenAI model names.

The proxy does one thing: it forwards requests to a configured upstream endpoint
and rewrites only the top-level JSON `model` field when it matches `model_map`.
It does not store, inject, or replace API keys. Client request headers, including
`Authorization`, are forwarded to the upstream service.

## What It Supports

- `/v1/responses`, including streamed Server-Sent Events responses.
- Other OpenAI-style `/v1/*` paths through transparent forwarding.
- macOS and Linux with Python 3.9+.
- Configuration through `config.json`, environment variables, or CLI flags.
- No runtime dependencies outside the Python standard library.

## Repository Layout

```text
.
├── minimal_openai_proxy.py   # HTTP server, path joining, model rewrite, forwarding
├── config.example.json       # Example endpoint and model map, no API key
├── tests/
│   └── test_proxy.py         # Local fake-upstream tests for rewrite and forwarding
├── README.md                 # User, configuration, and operations guide
└── .gitignore                # Ignores local config, logs, pid files, caches
```

## Request Flow

```text
client
  -> http://proxy:18080/v1/responses
  -> proxy rewrites {"model":"gpt-5.5"} to {"model":"enterprise-gpt-5.5"}
  -> https://your-enterprise-openai.example.com/v1/responses
```

The request body is otherwise preserved. Query strings, request method, and
ordinary headers are forwarded. Hop-by-hop headers such as `Connection` and
`Transfer-Encoding` are intentionally removed.

## Configuration

Copy the example config and edit it for the deployment:

```bash
cp config.example.json config.json
```

Example `config.json`:

```json
{
  "host": "127.0.0.1",
  "port": 8000,
  "target_base_url": "https://your-enterprise-openai.example.com/v1",
  "strip_prefix": "auto",
  "model_map": {
    "gpt-5.5": "enterprise-gpt-5.5",
    "gpt-5.4": "enterprise-gpt-5.4",
    "gpt-5.3": "enterprise-gpt-5.3"
  }
}
```

`config.json` is ignored by git so machine-specific settings stay local.

### Fields

- `host`: Local bind address. Use `127.0.0.1` for local-only use or `0.0.0.0`
  to expose the proxy on a server.
- `port`: Local listen port.
- `target_base_url`: Upstream OpenAI-compatible endpoint. It may include `/v1`.
- `strip_prefix`: Defaults to `auto`. If both the target and incoming path start
  with the same version prefix, the incoming prefix is stripped once so
  `/v1/responses` plus a target ending in `/v1` becomes `/v1/responses`, not
  `/v1/v1/responses`.
- `model_map`: Mapping from client-facing model names to upstream model names.

### Configuration Precedence

For scalar settings such as `target_base_url`, `host`, `port`, `timeout`, and
`strip_prefix`, CLI flags override environment variables, which override
`config.json`.

For `model_map`, entries are merged in this order:

```text
config.json < OPENAI_PROXY_MODEL_MAP < --model-map
```

Later entries with the same key override earlier entries.

### Environment Variables

```bash
export OPENAI_PROXY_TARGET_BASE_URL="https://your-enterprise-openai.example.com/v1"
export OPENAI_PROXY_MODEL_MAP='{"gpt-5.5":"enterprise-gpt-5.5","gpt-5.4":"enterprise-gpt-5.4","gpt-5.3":"enterprise-gpt-5.3"}'
export OPENAI_PROXY_HOST="127.0.0.1"
export OPENAI_PROXY_PORT="18080"
python3 minimal_openai_proxy.py
```

### CLI-Only Startup

```bash
python3 minimal_openai_proxy.py \
  --host 127.0.0.1 \
  --port 18080 \
  --target-base-url https://your-enterprise-openai.example.com/v1 \
  --model-map '{"gpt-5.5":"enterprise-gpt-5.5","gpt-5.4":"enterprise-gpt-5.4","gpt-5.3":"enterprise-gpt-5.3"}'
```

## Client Usage

Point OpenAI-compatible clients at the proxy `/v1` base URL:

```text
http://127.0.0.1:18080/v1
```

Example Responses API call:

```bash
curl http://127.0.0.1:18080/v1/responses \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.5","input":"Say OK in one word.","max_output_tokens":64}'
```

Example streaming call:

```bash
curl -N http://127.0.0.1:18080/v1/responses \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.5","input":"Say OK in one word.","max_output_tokens":64,"stream":true}'
```

For Codex or other OpenAI SDK-compatible tools, use:

```bash
export OPENAI_BASE_URL="http://127.0.0.1:18080/v1"
export OPENAI_API_KEY="<enterprise key>"
```

## Local Development

Run tests:

```bash
python3 -m unittest discover -s tests
```

Run syntax checks:

```bash
python3 -m py_compile minimal_openai_proxy.py tests/test_proxy.py
```

Start locally:

```bash
python3 minimal_openai_proxy.py --host 127.0.0.1 --port 18080
```

Health check:

```bash
curl http://127.0.0.1:18080/healthz
```

The health response is intentionally minimal:

```json
{"ok":true}
```

## Linux Server Operations

The current simple deployment uses `nohup` and a pid file. It is intentionally
plain so it works on minimal Linux machines without a package manager or service
manager setup.

### Clone

```bash
cd ~/workspace
git clone git@github.com:netaddi/minimal_openai_proxy.git
cd minimal_openai_proxy
cp config.example.json config.json
```

Edit `config.json` as needed. Do not add API keys to it.

### Start

```bash
cd ~/workspace/minimal_openai_proxy
nohup python3 minimal_openai_proxy.py --host 127.0.0.1 --port 18080 > proxy.log 2>&1 &
echo $! > proxy.pid
```

This binds to localhost by default. Use the SSH tunnel section below for local
client access from your workstation.

To expose the service on a server interface, bind `0.0.0.0` only behind network
controls such as a firewall, trusted reverse proxy, or private subnet ACL:

```bash
nohup python3 minimal_openai_proxy.py --host 0.0.0.0 --port 18080 > proxy.log 2>&1 &
echo $! > proxy.pid
```

### Check Status

```bash
cat ~/workspace/minimal_openai_proxy/proxy.pid
ps -fp "$(cat ~/workspace/minimal_openai_proxy/proxy.pid)"
curl http://127.0.0.1:18080/healthz
tail -f ~/workspace/minimal_openai_proxy/proxy.log
```

### Stop

```bash
kill "$(cat ~/workspace/minimal_openai_proxy/proxy.pid)"
rm -f ~/workspace/minimal_openai_proxy/proxy.pid
```

### Restart

```bash
cd ~/workspace/minimal_openai_proxy
kill "$(cat proxy.pid)" 2>/dev/null || true
nohup python3 minimal_openai_proxy.py --host 127.0.0.1 --port 18080 > proxy.log 2>&1 &
echo $! > proxy.pid
```

### Update

```bash
cd ~/workspace/minimal_openai_proxy
git pull --ff-only
python3 -m unittest discover -s tests
kill "$(cat proxy.pid)" 2>/dev/null || true
nohup python3 minimal_openai_proxy.py --host 127.0.0.1 --port 18080 > proxy.log 2>&1 &
echo $! > proxy.pid
```

## macOS Operations

For local foreground use:

```bash
python3 minimal_openai_proxy.py --host 127.0.0.1 --port 18080
```

For a local background process:

```bash
nohup python3 minimal_openai_proxy.py --host 127.0.0.1 --port 18080 > proxy.log 2>&1 &
echo $! > proxy.pid
```

Stop it with:

```bash
kill "$(cat proxy.pid)"
rm -f proxy.pid
```

## Remote Access Through SSH

If the server listens only on localhost or the server port is not reachable
directly, use an SSH tunnel:

```bash
ssh -L 18080:127.0.0.1:18080 3090x8
```

Then configure clients with:

```text
OPENAI_BASE_URL=http://127.0.0.1:18080/v1
```

## Troubleshooting

- `404` or upstream path errors: check `target_base_url` and `strip_prefix`.
  With a target ending in `/v1`, the usual client base URL should also end in
  `/v1`, and `strip_prefix` should normally remain `auto`.
- `401` or `403`: check the client-sent `Authorization` header. The proxy does
  not inject keys.
- Streaming hangs or disconnects: inspect `proxy.log` and retry with a small
  non-streaming `/v1/responses` request first.
- Port already in use: choose another `--port` or stop the existing listener.
- Wrong model at upstream: check `config.json` or `OPENAI_PROXY_MODEL_MAP` and
  watch `proxy.log`; rewrite events are logged as `rewrite=old->new`.

## Security Notes

- Do not commit `config.json`, `proxy.log`, or `proxy.pid`.
- Do not put API keys in config files, command examples, logs, or commits.
- This proxy performs no authentication of its own. If it is bound to `0.0.0.0`,
  protect it with network controls, firewall rules, SSH tunneling, or a trusted
  reverse proxy.
