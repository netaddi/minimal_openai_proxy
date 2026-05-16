#!/usr/bin/env python3
"""A tiny OpenAI-compatible proxy that rewrites only the top-level model field."""

from __future__ import annotations

import argparse
import dataclasses
import http.client
import http.server
import json
import os
import socketserver
import sys
import urllib.parse
from typing import Dict, Optional, Tuple


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


@dataclasses.dataclass(frozen=True)
class ProxyConfig:
    target_base_url: str
    model_map: Dict[str, str]
    host: str = "127.0.0.1"
    port: int = 8000
    timeout_seconds: float = 600.0
    strip_prefix: str = "auto"
    chunk_size: int = 8192


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def load_config_file(path: Optional[str]) -> dict:
    if not path:
        return {}
    if not os.path.exists(path):
        raise FileNotFoundError(f"config file not found: {path}")
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def parse_model_map(raw: Optional[str]) -> Dict[str, str]:
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise ValueError("model map must be a JSON object of string-to-string entries")
    return value


def build_config(args: argparse.Namespace) -> ProxyConfig:
    config_path = args.config
    if config_path is None and os.path.exists("config.json"):
        config_path = "config.json"
    file_config = load_config_file(config_path)

    target_base_url = (
        args.target_base_url
        or os.getenv("OPENAI_PROXY_TARGET_BASE_URL")
        or file_config.get("target_base_url")
    )
    if not target_base_url:
        raise ValueError(
            "target endpoint is required. Set --target-base-url, "
            "OPENAI_PROXY_TARGET_BASE_URL, or config.json target_base_url."
        )

    model_map = {}
    if isinstance(file_config.get("model_map"), dict):
        model_map.update(file_config["model_map"])
    model_map.update(parse_model_map(os.getenv("OPENAI_PROXY_MODEL_MAP")))
    model_map.update(parse_model_map(args.model_map))

    host = args.host or os.getenv("OPENAI_PROXY_HOST") or file_config.get("host") or "127.0.0.1"
    port = int(args.port or os.getenv("OPENAI_PROXY_PORT") or file_config.get("port") or 8000)
    timeout_seconds = float(
        args.timeout
        or os.getenv("OPENAI_PROXY_TIMEOUT_SECONDS")
        or file_config.get("timeout_seconds")
        or 600
    )
    strip_prefix = (
        args.strip_prefix
        if args.strip_prefix is not None
        else os.getenv("OPENAI_PROXY_STRIP_PREFIX", file_config.get("strip_prefix", "auto"))
    )

    return ProxyConfig(
        target_base_url=target_base_url.rstrip("/"),
        model_map=model_map,
        host=host,
        port=port,
        timeout_seconds=timeout_seconds,
        strip_prefix=strip_prefix,
    )


def should_strip_auto(target_base_url: str, incoming_path: str) -> bool:
    base_path = urllib.parse.urlsplit(target_base_url).path.rstrip("/")
    base_last = base_path.rsplit("/", 1)[-1] if base_path else ""
    incoming_first = incoming_path.strip("/").split("/", 1)[0] if incoming_path.strip("/") else ""
    return bool(base_last and incoming_first and base_last == incoming_first)


def strip_incoming_prefix(config: ProxyConfig, incoming_path: str) -> str:
    if config.strip_prefix == "auto":
        if should_strip_auto(config.target_base_url, incoming_path):
            parts = incoming_path.strip("/").split("/", 1)
            return f"/{parts[1]}" if len(parts) == 2 else "/"
        return incoming_path

    if not config.strip_prefix:
        return incoming_path

    prefix = "/" + config.strip_prefix.strip("/")
    if incoming_path == prefix:
        return "/"
    if incoming_path.startswith(prefix + "/"):
        return incoming_path[len(prefix) :]
    return incoming_path


def build_target_request(config: ProxyConfig, request_path: str) -> Tuple[urllib.parse.SplitResult, str]:
    base = urllib.parse.urlsplit(config.target_base_url)
    incoming = urllib.parse.urlsplit(request_path)
    incoming_path = strip_incoming_prefix(config, incoming.path)

    target_path = "/".join(
        part.strip("/") for part in (base.path, incoming_path) if part and part.strip("/")
    )
    target_path = "/" + target_path if target_path else "/"
    if incoming.query:
        target_path = f"{target_path}?{incoming.query}"
    return base, target_path


def rewrite_json_model(body: bytes, content_type: str, model_map: Dict[str, str]) -> Tuple[bytes, Optional[Tuple[str, str]]]:
    if not body or "application/json" not in content_type.lower():
        return body, None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body, None

    if not isinstance(payload, dict):
        return body, None

    old_model = payload.get("model")
    if not isinstance(old_model, str) or old_model not in model_map:
        return body, None

    new_model = model_map[old_model]
    payload["model"] = new_model
    rewritten = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return rewritten, (old_model, new_model)


def copy_request_headers(handler: http.server.BaseHTTPRequestHandler, body: bytes, target_host: str) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for key, value in handler.headers.items():
        lower = key.lower()
        if lower in HOP_BY_HOP_HEADERS or lower in {"host", "content-length"}:
            continue
        headers[key] = value
    headers["Host"] = target_host
    if body:
        headers["Content-Length"] = str(len(body))
    return headers


def copy_response_headers(
    handler: http.server.BaseHTTPRequestHandler,
    upstream_response: http.client.HTTPResponse,
) -> None:
    for key, value in upstream_response.getheaders():
        lower = key.lower()
        if lower in HOP_BY_HOP_HEADERS or lower == "content-length":
            continue
        handler.send_header(key, value)
    handler.send_header("Connection", "close")


def make_handler(config: ProxyConfig):
    class OpenAIProxyHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "minimal-openai-proxy/0.1"

        def log_message(self, fmt: str, *args) -> None:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def do_GET(self) -> None:
            if self.path in {"/", "/healthz"}:
                self.respond_health()
                return
            self.proxy_request()

        def do_POST(self) -> None:
            self.proxy_request()

        def do_DELETE(self) -> None:
            self.proxy_request()

        def do_PUT(self) -> None:
            self.proxy_request()

        def do_PATCH(self) -> None:
            self.proxy_request()

        def do_OPTIONS(self) -> None:
            self.proxy_request()

        def respond_health(self) -> None:
            payload = json.dumps(
                {
                    "ok": True,
                    "target_base_url": config.target_base_url,
                    "mapped_models": sorted(config.model_map.keys()),
                },
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True

        def read_body(self) -> bytes:
            content_length = self.headers.get("Content-Length")
            if not content_length:
                return b""
            return self.rfile.read(int(content_length))

        def proxy_request(self) -> None:
            base, target_path = build_target_request(config, self.path)
            body, rewrite = rewrite_json_model(
                self.read_body(),
                self.headers.get("Content-Type", ""),
                config.model_map,
            )
            target_host = base.netloc
            headers = copy_request_headers(self, body, target_host)

            connection_cls = http.client.HTTPSConnection if base.scheme == "https" else http.client.HTTPConnection
            connection = connection_cls(target_host, timeout=config.timeout_seconds)
            status = 502
            try:
                connection.request(self.command, target_path, body=body or None, headers=headers)
                upstream = connection.getresponse()
                status = upstream.status
                self.send_response(upstream.status, upstream.reason)
                copy_response_headers(self, upstream)
                self.end_headers()
                while True:
                    chunk = upstream.read(config.chunk_size)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except BrokenPipeError:
                pass
            except Exception as exc:
                if not self.headers_sent():
                    self.respond_proxy_error(exc)
                else:
                    self.log_error("upstream error after headers were sent: %r", exc)
            finally:
                connection.close()
                self.close_connection = True
                rewrite_msg = f" rewrite={rewrite[0]}->{rewrite[1]}" if rewrite else ""
                self.log_message('"%s %s" %s%s', self.command, self.path, status, rewrite_msg)

        def headers_sent(self) -> bool:
            return getattr(self, "_headers_buffer", None) == []

        def respond_proxy_error(self, exc: Exception) -> None:
            payload = json.dumps(
                {"error": {"message": f"proxy upstream request failed: {exc}", "type": "proxy_error"}},
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)

    return OpenAIProxyHandler


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal OpenAI API proxy with model-name rewriting.")
    parser.add_argument("--config", help="JSON config path. Defaults to ./config.json when it exists.")
    parser.add_argument("--host", help="Listen host. Defaults to config/env or 127.0.0.1.")
    parser.add_argument("--port", type=int, help="Listen port. Defaults to config/env or 8000.")
    parser.add_argument("--target-base-url", help="Upstream OpenAI-compatible endpoint base URL.")
    parser.add_argument("--model-map", help='JSON object, for example {"gpt-5.5":"custom-model"}.')
    parser.add_argument("--timeout", type=float, help="Upstream timeout in seconds.")
    parser.add_argument(
        "--strip-prefix",
        help='Incoming path prefix to strip before joining target URL. Use "" to disable. Defaults to auto.',
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    try:
        config = build_config(parse_args(argv))
    except Exception as exc:
        sys.stderr.write(f"configuration error: {exc}\n")
        return 2

    handler = make_handler(config)
    with ThreadingHTTPServer((config.host, config.port), handler) as server:
        sys.stderr.write(
            f"minimal-openai-proxy listening on http://{config.host}:{config.port} "
            f"-> {config.target_base_url}\n"
        )
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
