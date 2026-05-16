# minimal_openai_proxy

A minimal OpenAI-compatible HTTP proxy that rewrites only the top-level JSON
`model` field and forwards everything else to a configured upstream endpoint.

It is intended for environments where an enterprise OpenAI-compatible endpoint
uses custom model names, while local tools expect official OpenAI model names.

## Features

- No API key in config. `Authorization` and other request headers are forwarded
  from the client request.
- Supports `/v1/responses` and other OpenAI-style `/v1/*` paths by transparent
  forwarding.
- Supports streaming responses because upstream response bodies are copied as
  they arrive.
- Uses only the Python standard library.
- Works on macOS and Linux with Python 3.9+.

## Quick start

```bash
cp config.example.json config.json
python3 minimal_openai_proxy.py --host 0.0.0.0 --port 8000
```

Point OpenAI-compatible clients at:

```text
http://127.0.0.1:8000/v1
```

Example request:

```bash
curl http://127.0.0.1:8000/v1/responses \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.5","input":"hello"}'
```

With the example config, the upstream request is sent to:

```text
https://idealab.alibaba-inc.com/api/openai/v1/responses
```

and the body model is rewritten from `gpt-5.5` to
`gpt-5.5-0424-global`.

## Configuration

Use `config.json`:

```json
{
  "host": "127.0.0.1",
  "port": 8000,
  "target_base_url": "https://idealab.alibaba-inc.com/api/openai/v1",
  "strip_prefix": "auto",
  "model_map": {
    "gpt-5.5": "gpt-5.5-0424-global",
    "gpt-5.4": "gpt-5.4-0305-global",
    "gpt-5.3": "gpt-5.3-chat-0303-global"
  }
}
```

Or environment variables:

```bash
export OPENAI_PROXY_TARGET_BASE_URL="https://idealab.alibaba-inc.com/api/openai/v1"
export OPENAI_PROXY_MODEL_MAP='{"gpt-5.5":"gpt-5.5-0424-global","gpt-5.4":"gpt-5.4-0305-global","gpt-5.3":"gpt-5.3-chat-0303-global"}'
python3 minimal_openai_proxy.py --host 0.0.0.0 --port 8000
```

The proxy does not store or inject API keys. Configure your client to send the
enterprise key in the normal OpenAI `Authorization: Bearer ...` header.

## Path handling

If `target_base_url` ends in `/v1` and the client requests `/v1/responses`, the
proxy automatically strips the incoming `/v1` once before joining paths. This
avoids forwarding to `/v1/v1/responses`.

Set `"strip_prefix": ""` to disable prefix stripping.

## Health check

```bash
curl http://127.0.0.1:8000/healthz
```

## Test

```bash
python3 -m unittest discover -s tests
```
