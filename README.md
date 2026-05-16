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
├── pricing.example.json      # Example token-price table for cost calculation
├── scripts/
│   └── calculate_cost.py     # Summarizes usage JSONL logs into cost
├── tests/
│   └── test_proxy.py         # Local fake-upstream tests for rewrite and forwarding
├── README.md                 # User, configuration, and operations guide
└── .gitignore                # Ignores local config, logs, pid files, caches
```

## Request Flow

```text
client
  -> http://proxy:18080/v1/responses
  -> proxy rewrites {"model":"gpt-5.5"} to {"model":"gpt-5.5-0424-global"}
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
  "host": "0.0.0.0",
  "port": 8000,
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

`config.json` is ignored by git so machine-specific settings stay local.

### Fields

- `host`: Local bind address. Defaults to `0.0.0.0`, so the proxy listens on
  all interfaces unless overridden.
- `port`: Local listen port.
- `target_base_url`: Upstream OpenAI-compatible endpoint. It may include `/v1`.
- `strip_prefix`: Defaults to `auto`. If both the target and incoming path start
  with the same version prefix, the incoming prefix is stripped once so
  `/v1/responses` plus a target ending in `/v1` becomes `/v1/responses`, not
  `/v1/v1/responses`.
- `model_map`: Mapping from client-facing model names to upstream model names.
- `usage_log_path`: JSONL usage log path. Use `""` or `null` to disable usage
  logging.
- `usage_capture_max_bytes`: Maximum non-stream response bytes held in memory
  only for usage parsing. Defaults to `1048576`.

### Configuration Precedence

For scalar settings such as `target_base_url`, `host`, `port`, `timeout`,
`strip_prefix`, `usage_log_path`, and `usage_capture_max_bytes`, CLI flags
override environment variables, which override `config.json`.

For `model_map`, entries are merged in this order:

```text
config.json < OPENAI_PROXY_MODEL_MAP < --model-map
```

Later entries with the same key override earlier entries.

### Environment Variables

```bash
export OPENAI_PROXY_TARGET_BASE_URL="https://your-enterprise-openai.example.com/v1"
export OPENAI_PROXY_MODEL_MAP='{"gpt-5.5":"gpt-5.5-0424-global","gpt-5.4":"gpt-5.4-0305-global","gpt-5.3":"gpt-5.3-chat-0303-global"}'
export OPENAI_PROXY_HOST="0.0.0.0"
export OPENAI_PROXY_PORT="18080"
export OPENAI_PROXY_USAGE_LOG="usage.jsonl"
python3 minimal_openai_proxy.py
```

### CLI-Only Startup

```bash
python3 minimal_openai_proxy.py \
  --host 0.0.0.0 \
  --port 18080 \
  --target-base-url https://your-enterprise-openai.example.com/v1 \
  --model-map '{"gpt-5.5":"gpt-5.5-0424-global","gpt-5.4":"gpt-5.4-0305-global","gpt-5.3":"gpt-5.3-chat-0303-global"}' \
  --usage-log usage.jsonl
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
python3 minimal_openai_proxy.py --host 0.0.0.0 --port 18080
```

Health check:

```bash
curl http://127.0.0.1:18080/healthz
```

The health response is intentionally minimal:

```json
{"ok":true}
```

## Usage Logging

Every proxied API call appends one JSON object to `usage_log_path`. Health checks
are not usage-logged. The log intentionally excludes request headers, request
body, response body, and API keys.

Useful fields:

- `timestamp`, `request_id`, `client_ip`, `method`, `path`.
- `status`, `duration_ms`, `request_bytes`, `response_bytes`.
- `request_model`: model name received from the client.
- `upstream_model`: model name sent to the upstream endpoint after mapping.
- `response_model`: model name returned by the upstream response.
- `model_rewritten`: whether the model was changed.
- `stream`: whether the request asked for streaming.
- `response_id`, `response_status`, `incomplete_reason`, `upstream_request_id`.
- `usage`: normalized token usage used by the cost script.
- `raw_usage`: numeric-only copy of the upstream usage object, preserving future
  token counters while dropping non-numeric custom telemetry.

Normalized `usage` fields currently include:

- `input_tokens`, `output_tokens`, `total_tokens`.
- `input_cached_read_tokens`: cache-hit input tokens, for providers that expose
  cached input reads.
- `input_cached_write_tokens`: cache-creation/write input tokens, for providers
  that expose them.
- `output_reasoning_tokens`: reasoning tokens included in output usage.
- `output_cached_read_tokens`.
- `input_audio_tokens`, `output_audio_tokens`.

Example record:

```json
{"timestamp":"2026-05-16T02:00:00.000Z","request_id":"...","method":"POST","path":"/v1/responses","status":200,"duration_ms":1234.5,"request_model":"gpt-5.5","upstream_model":"gpt-5.5-0424-global","response_model":"gpt-5.5-0424-global","model_rewritten":true,"stream":true,"usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15,"input_cached_read_tokens":3,"input_cached_write_tokens":0,"output_reasoning_tokens":2},"raw_usage":{"input_tokens":10,"output_tokens":5,"total_tokens":15}}
```

## Cost Calculation

Costs are calculated from the usage JSONL log and an external pricing table. The
proxy does not hard-code prices because enterprise endpoints often have private
rates, and future GPT versions should be handled by updating data rather than
editing code.

Copy and edit the example:

```bash
cp pricing.example.json pricing.json
```

Pricing file shape:

```json
{
  "currency": "USD",
  "unit_tokens": 1000000,
  "cached_read_semantics": "subset",
  "cached_write_semantics": "additive",
  "models": {
    "gpt-5.5-0424-global": {
      "input": 1.0,
      "input_cached_read": 0.25,
      "input_cached_write": 1.0,
      "output": 2.0
    }
  },
  "aliases": {
    "gpt-5.5": "gpt-5.5-0424-global"
  },
  "patterns": [
    {
      "glob": "gpt-5.*-global",
      "rates": {
        "input": 1.0,
        "input_cached_read": 0.25,
        "input_cached_write": 1.0,
        "output": 2.0
      }
    }
  ]
}
```

Run:

```bash
python3 scripts/calculate_cost.py --pricing pricing.json usage.jsonl
python3 scripts/calculate_cost.py --pricing pricing.json --json usage.jsonl
```

The calculator chooses `response_model`, then `upstream_model`, then
`request_model` by default. Override this if you prefer billing by the public
model name:

```bash
python3 scripts/calculate_cost.py --pricing pricing.json --model-field request_model usage.jsonl
```

Pricing behavior:

- `models` exact matches win first.
- `aliases` map response model names to a priced model.
- `patterns` can use `glob` or regex `pattern` so future model families can be
  priced without changing code. Regex patterns use full-match semantics; write
  `.*gpt-5.*` if you intentionally want substring matching.
- Cached-read/write tokens are subtracted from regular input only when their
  separate rates are present and their semantics are `subset`.
- `cached_read_semantics` defaults to `subset`, matching OpenAI-style
  `cached_tokens` fields where cached reads are included in total input tokens.
- `cached_write_semantics` defaults to `additive`, matching providers that
  report cache creation/write tokens as a separate pool. Override these fields
  globally or per model if your enterprise endpoint reports usage differently.
- Reasoning tokens are included in output price unless `output_reasoning` is
  explicitly configured as a separate rate.

## Linux Server Operations

The current simple deployment uses `nohup` and a pid file. It is intentionally
plain so it works on minimal Linux machines without a package manager or service
manager setup.

### Clone

```bash
cd ~/workspace
git clone git@gitlab.alibaba-inc.com:wangyin.yx/minimal_openai_proxy.git
cd minimal_openai_proxy
cp config.example.json config.json
```

Edit `config.json` as needed. Do not add API keys to it.

### Start

```bash
cd ~/workspace/minimal_openai_proxy
nohup python3 minimal_openai_proxy.py --host 0.0.0.0 --port 18080 > proxy.log 2>&1 &
echo $! > proxy.pid
```

This binds to all interfaces by default. If you only want local access through
SSH tunneling, set `--host 127.0.0.1`.

Important: the proxy does not authenticate clients by itself. Binding
`0.0.0.0` is convenient for shared internal hosts, but it should only be used on
a trusted network or behind firewall/reverse-proxy controls.

### Check Status

```bash
cat ~/workspace/minimal_openai_proxy/proxy.pid
ps -fp "$(cat ~/workspace/minimal_openai_proxy/proxy.pid)"
curl http://127.0.0.1:18080/healthz
tail -f ~/workspace/minimal_openai_proxy/proxy.log
tail -f ~/workspace/minimal_openai_proxy/usage.jsonl
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
nohup python3 minimal_openai_proxy.py --host 0.0.0.0 --port 18080 > proxy.log 2>&1 &
echo $! > proxy.pid
```

### Update

```bash
cd ~/workspace/minimal_openai_proxy
git pull --ff-only
python3 -m unittest discover -s tests
kill "$(cat proxy.pid)" 2>/dev/null || true
nohup python3 minimal_openai_proxy.py --host 0.0.0.0 --port 18080 > proxy.log 2>&1 &
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
ssh -L 18080:127.0.0.1:18080 your-server
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
- Usage is empty for a response: check `usage_parse_warning` in `usage.jsonl`.
  The proxy strips `Accept-Encoding` before forwarding requests to keep upstream
  usage parseable, but a provider may still force compressed responses.
- Port already in use: choose another `--port` or stop the existing listener.
- Wrong model at upstream: check `config.json` or `OPENAI_PROXY_MODEL_MAP` and
  watch `proxy.log`; rewrite events are logged as `rewrite=old->new`.
- Cost is zero or missing: fill real rates in `pricing.json` and make sure the
  billing model matches `response_model`, an `aliases` entry, or a `patterns`
  rule.

## Security Notes

- Do not commit `config.json`, `proxy.log`, `usage.jsonl`, or `proxy.pid`.
- Do not put API keys in config files, command examples, logs, or commits.
- This proxy performs no authentication of its own and defaults to `0.0.0.0`.
  Protect it with network controls, firewall rules, SSH tunneling, or a trusted
  reverse proxy.
