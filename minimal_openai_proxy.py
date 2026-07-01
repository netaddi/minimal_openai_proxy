#!/usr/bin/env python3
"""A tiny OpenAI-compatible proxy that rewrites only the top-level model field."""

from __future__ import annotations

import argparse
import codecs
import dataclasses
import datetime
import fcntl
import http.client
import http.server
import json
import math
import os
import socketserver
import sys
import threading
import time
import urllib.parse
import uuid
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


class RequestBodyError(Exception):
    """Raised when a client request body cannot be forwarded safely."""

    def __init__(self, message: str, status: int, error_type: str) -> None:
        super().__init__(message)
        self.status = status
        self.error_type = error_type


USAGE_LOG_LOCK = threading.Lock()


@dataclasses.dataclass(frozen=True)
class ProxyConfig:
    target_base_url: str
    model_map: Dict[str, str]
    host: str = "0.0.0.0"
    port: int = 8000
    timeout_seconds: float = 600.0
    strip_prefix: str = "auto"
    chunk_size: int = 8192
    usage_log_path: Optional[str] = "usage.jsonl"
    usage_capture_max_bytes: int = 1024 * 1024


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
    if not isinstance(value, dict):
        raise ValueError("model map must be a JSON object of string-to-string entries")
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str) or not key or not item:
            raise ValueError("model map must contain non-empty string-to-string entries")
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

    host = args.host or os.getenv("OPENAI_PROXY_HOST") or file_config.get("host") or "0.0.0.0"
    port_value = args.port if args.port is not None else os.getenv("OPENAI_PROXY_PORT", file_config.get("port", 8000))
    port = int(port_value)
    timeout_value = (
        args.timeout
        if args.timeout is not None
        else os.getenv("OPENAI_PROXY_TIMEOUT_SECONDS", file_config.get("timeout_seconds", 600))
    )
    timeout_seconds = float(timeout_value)
    strip_prefix = (
        args.strip_prefix
        if args.strip_prefix is not None
        else os.getenv("OPENAI_PROXY_STRIP_PREFIX", file_config.get("strip_prefix", "auto"))
    )
    usage_log_path = (
        args.usage_log
        if args.usage_log is not None
        else os.getenv("OPENAI_PROXY_USAGE_LOG", file_config.get("usage_log_path", "usage.jsonl"))
    )
    if usage_log_path in {"", "none", "None", "null"}:
        usage_log_path = None
    usage_capture_value = (
        args.usage_capture_max_bytes
        if args.usage_capture_max_bytes is not None
        else os.getenv(
            "OPENAI_PROXY_USAGE_CAPTURE_MAX_BYTES",
            file_config.get("usage_capture_max_bytes", 64 * 1024 * 1024),
        )
    )
    usage_capture_max_bytes = int(usage_capture_value)
    if not 0 < port < 65536:
        raise ValueError("port must be between 1 and 65535")
    if usage_capture_max_bytes < 0:
        raise ValueError("usage_capture_max_bytes must be >= 0")

    return ProxyConfig(
        target_base_url=target_base_url.rstrip("/"),
        model_map=model_map,
        host=host,
        port=port,
        timeout_seconds=timeout_seconds,
        strip_prefix=strip_prefix,
        usage_log_path=usage_log_path,
        usage_capture_max_bytes=usage_capture_max_bytes,
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


def parse_json_body(body: bytes, content_type: str) -> Optional[dict]:
    if not body or "application/json" not in content_type.lower():
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None
    return payload


def rewrite_json_model(body: bytes, content_type: str, model_map: Dict[str, str]) -> Tuple[bytes, Optional[Tuple[str, str]]]:
    payload = parse_json_body(body, content_type)
    if payload is None:
        return body, None

    old_model = payload.get("model")
    if not isinstance(old_model, str) or old_model not in model_map:
        return body, None

    new_model = model_map[old_model]
    payload["model"] = new_model
    rewritten = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return rewritten, (old_model, new_model)


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def int_value(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def nested_int(container: dict, *path: str) -> int:
    value = container
    for key in path:
        if not isinstance(value, dict):
            return 0
        value = value.get(key)
    return int_value(value)


def normalize_usage(raw_usage: Optional[dict]) -> dict:
    if not isinstance(raw_usage, dict):
        return {}

    input_tokens = (
        nested_int(raw_usage, "input_tokens")
        or nested_int(raw_usage, "prompt_tokens")
        or nested_int(raw_usage, "input")
    )
    output_tokens = (
        nested_int(raw_usage, "output_tokens")
        or nested_int(raw_usage, "completion_tokens")
        or nested_int(raw_usage, "output")
    )
    input_cached_read_tokens = (
        nested_int(raw_usage, "input_tokens_details", "cached_tokens")
        or nested_int(raw_usage, "prompt_tokens_details", "cached_tokens")
        or nested_int(raw_usage, "cache_read_input_tokens")
        or nested_int(raw_usage, "input_cached_tokens")
    )
    input_cached_write_tokens = (
        nested_int(raw_usage, "input_tokens_details", "cache_write_tokens")
        or nested_int(raw_usage, "prompt_tokens_details", "cache_write_tokens")
        or nested_int(raw_usage, "cache_creation_input_tokens")
        or nested_int(raw_usage, "input_cache_write_tokens")
    )
    output_reasoning_tokens = (
        nested_int(raw_usage, "output_tokens_details", "reasoning_tokens")
        or nested_int(raw_usage, "completion_tokens_details", "reasoning_tokens")
        or nested_int(raw_usage, "reasoning_tokens")
    )
    output_cached_read_tokens = (
        nested_int(raw_usage, "output_tokens_details", "cached_tokens")
        or nested_int(raw_usage, "completion_tokens_details", "cached_tokens")
        or nested_int(raw_usage, "output_cached_tokens")
    )
    input_audio_tokens = (
        nested_int(raw_usage, "input_tokens_details", "audio_tokens")
        or nested_int(raw_usage, "prompt_tokens_details", "audio_tokens")
    )
    output_audio_tokens = (
        nested_int(raw_usage, "output_tokens_details", "audio_tokens")
        or nested_int(raw_usage, "completion_tokens_details", "audio_tokens")
    )
    total_tokens = nested_int(raw_usage, "total_tokens")
    if not total_tokens:
        total_tokens = input_tokens + output_tokens + input_cached_read_tokens + input_cached_write_tokens

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "input_cached_read_tokens": input_cached_read_tokens,
        "input_cached_write_tokens": input_cached_write_tokens,
        "output_reasoning_tokens": output_reasoning_tokens,
        "output_cached_read_tokens": output_cached_read_tokens,
        "input_audio_tokens": input_audio_tokens,
        "output_audio_tokens": output_audio_tokens,
    }


def sanitize_usage(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        return value
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            sanitized_item = sanitize_usage(item)
            if sanitized_item is not None:
                sanitized[key] = sanitized_item
        return sanitized
    if isinstance(value, list):
        sanitized_list = [sanitize_usage(item) for item in value]
        sanitized_list = [item for item in sanitized_list if item is not None]
        return sanitized_list or None
    return None


def extract_response_info(payload: object) -> dict:
    if not isinstance(payload, dict):
        return {}

    response = payload.get("response") if isinstance(payload.get("response"), dict) else payload
    if not isinstance(response, dict):
        return {}

    info = {
        "response_id": response.get("id") if isinstance(response.get("id"), str) else None,
        "response_model": response.get("model") if isinstance(response.get("model"), str) else None,
        "response_status": response.get("status") if isinstance(response.get("status"), str) else None,
        "raw_usage": sanitize_usage(response.get("usage")) if isinstance(response.get("usage"), dict) else None,
    }
    incomplete = response.get("incomplete_details")
    if isinstance(incomplete, dict):
        reason = incomplete.get("reason")
        if isinstance(reason, str):
            info["incomplete_reason"] = reason
    error = response.get("error")
    if isinstance(error, dict):
        error_type = error.get("type")
        error_code = error.get("code")
        error_message = error.get("message")
        if isinstance(error_type, str):
            info["response_error_type"] = error_type
        if isinstance(error_code, (str, int)):
            info["response_error_code"] = error_code
        if isinstance(error_message, str):
            info["response_error_message"] = error_message
    elif isinstance(payload.get("error"), dict):
        error_type = payload["error"].get("type")
        error_code = payload["error"].get("code")
        error_message = payload["error"].get("message")
        if isinstance(error_type, str):
            info["response_error_type"] = error_type
        if isinstance(error_code, (str, int)):
            info["response_error_code"] = error_code
        if isinstance(error_message, str):
            info["response_error_message"] = error_message
    return info


class SSEUsageTracker:
    def __init__(self, max_buffer_bytes: int) -> None:
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.max_buffer_bytes = max_buffer_bytes
        self.buffer = ""
        self.event_name = ""
        self.data_lines = []
        self.info: dict = {}

    def feed(self, chunk: bytes) -> None:
        self.buffer += self.decoder.decode(chunk)
        if self.max_buffer_bytes and len(self.buffer) > self.max_buffer_bytes:
            self.info["usage_parse_warning"] = "sse_event_too_large"
            self.buffer = ""
            self.event_name = ""
            self.data_lines = []
            return
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self._process_line(line.rstrip("\r"))

    def finish(self) -> dict:
        self.buffer += self.decoder.decode(b"", final=True)
        if self.buffer:
            self._process_line(self.buffer.rstrip("\r"))
            self.buffer = ""
        self._dispatch_event()
        return self.info

    def _process_line(self, line: str) -> None:
        if line == "":
            self._dispatch_event()
            return
        if line.startswith(":"):
            return
        if line.startswith("event:"):
            self.event_name = line[6:].strip()
            return
        if line.startswith("data:"):
            data = line[5:]
            if data.startswith(" "):
                data = data[1:]
            self.data_lines.append(data)
            if self.max_buffer_bytes and sum(len(item) for item in self.data_lines) > self.max_buffer_bytes:
                self.info["usage_parse_warning"] = "sse_event_too_large"
                self.event_name = ""
                self.data_lines = []

    def _dispatch_event(self) -> None:
        if not self.data_lines:
            self.event_name = ""
            return
        data = "\n".join(self.data_lines)
        self.event_name = ""
        self.data_lines = []
        if data == "[DONE]":
            return
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            return
        info = extract_response_info(payload)
        if info:
            self.info.update({key: value for key, value in info.items() if value is not None})


def write_usage_log(config: ProxyConfig, record: dict) -> None:
    if not config.usage_log_path:
        return
    directory = os.path.dirname(config.usage_log_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with USAGE_LOG_LOCK:
        with open(config.usage_log_path, "a", encoding="utf-8") as file:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX)
            try:
                file.write(line + "\n")
                file.flush()
            finally:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def copy_request_headers(handler: http.server.BaseHTTPRequestHandler, body: bytes, target_host: str) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for key, value in handler.headers.items():
        lower = key.lower()
        if lower in HOP_BY_HOP_HEADERS or lower in {"host", "content-length", "accept-encoding"}:
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
            payload = json.dumps({"ok": True}, separators=(",", ":")).encode("utf-8")
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
                if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                    raise RequestBodyError(
                        "chunked request bodies are not supported",
                        501,
                        "unsupported_request_body",
                    )
                return b""
            try:
                length = int(content_length)
            except ValueError as exc:
                raise RequestBodyError("invalid Content-Length", 400, "invalid_request_body") from exc
            if length < 0:
                raise RequestBodyError("invalid Content-Length", 400, "invalid_request_body")
            return self.rfile.read(length)

        def proxy_request(self) -> None:
            request_id = uuid.uuid4().hex
            started_at = time.monotonic()
            status = 502
            rewrite = None
            request_body = b""
            request_payload = None
            response_info: dict = {}
            raw_usage = None
            response_bytes = 0
            response_capture = bytearray()
            request_content_type = self.headers.get("Content-Type", "")
            try:
                request_body = self.read_body()
            except RequestBodyError as exc:
                status = exc.status
                self.log_usage_record(
                    request_id=request_id,
                    started_at=started_at,
                    status=status,
                    request_payload=None,
                    rewrite=None,
                    response_info={"response_error_type": exc.error_type},
                    raw_usage=None,
                    response_bytes=0,
                    request_bytes=0,
                )
                self.respond_json_error(status, str(exc), exc.error_type)
                self.log_message('"%s %s" %s', self.command, self.path, status)
                return

            request_payload = parse_json_body(request_body, request_content_type)
            base, target_path = build_target_request(config, self.path)
            body, rewrite = rewrite_json_model(
                request_body,
                request_content_type,
                config.model_map,
            )
            target_host = base.netloc
            headers = copy_request_headers(self, body, target_host)

            connection_cls = http.client.HTTPSConnection if base.scheme == "https" else http.client.HTTPConnection
            connection = connection_cls(target_host, timeout=config.timeout_seconds)
            headers_committed = False
            try:
                connection.request(self.command, target_path, body=body or None, headers=headers)
                upstream = connection.getresponse()
                status = upstream.status
                upstream_request_id = (
                    upstream.getheader("x-request-id")
                    or upstream.getheader("request-id")
                    or upstream.getheader("openai-request-id")
                )
                if upstream_request_id:
                    response_info["upstream_request_id"] = upstream_request_id
                response_content_type = upstream.getheader("Content-Type", "")
                content_encoding = upstream.getheader("Content-Encoding", "")
                is_event_stream = "text/event-stream" in response_content_type.lower()
                sse_tracker = SSEUsageTracker(config.usage_capture_max_bytes) if is_event_stream else None
                if is_event_stream:
                    response_info["observed_stream"] = True
                if content_encoding and "identity" not in content_encoding.lower():
                    response_info["usage_parse_warning"] = f"unsupported_content_encoding:{content_encoding}"
                self.send_response(upstream.status, upstream.reason)
                copy_response_headers(self, upstream)
                self.end_headers()
                headers_committed = True
                while True:
                    chunk = upstream.read1(config.chunk_size)
                    if not chunk:
                        break
                    response_bytes += len(chunk)
                    if sse_tracker is not None:
                        sse_tracker.feed(chunk)
                    elif (
                        (not content_encoding or "identity" in content_encoding.lower())
                        and len(response_capture) < config.usage_capture_max_bytes
                    ):
                        remaining = config.usage_capture_max_bytes - len(response_capture)
                        response_capture.extend(chunk[:remaining])
                    self.wfile.write(chunk)
                    self.wfile.flush()
                if sse_tracker is not None:
                    response_info.update(sse_tracker.finish())
                elif response_capture:
                    try:
                        response_payload = json.loads(response_capture.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        response_payload = None
                    response_info.update(extract_response_info(response_payload))
                    if response_bytes > len(response_capture):
                        response_info["usage_parse_warning"] = "response_capture_truncated"
                raw_usage = response_info.get("raw_usage")
            except BrokenPipeError:
                pass
            except Exception as exc:
                response_info["response_error_type"] = "proxy_error"
                if not headers_committed:
                    self.respond_proxy_error(exc)
                else:
                    self.log_error("upstream error after headers were sent: %r", exc)
            finally:
                connection.close()
                self.close_connection = True
                self.log_usage_record(
                    request_id=request_id,
                    started_at=started_at,
                    status=status,
                    request_payload=request_payload,
                    rewrite=rewrite,
                    response_info=response_info,
                    raw_usage=raw_usage,
                    response_bytes=response_bytes,
                    request_bytes=len(request_body),
                )
                rewrite_msg = f" rewrite={rewrite[0]}->{rewrite[1]}" if rewrite else ""
                self.log_message('"%s %s" %s%s', self.command, self.path, status, rewrite_msg)

        def log_usage_record(
            self,
            request_id: str,
            started_at: float,
            status: int,
            request_payload: Optional[dict],
            rewrite: Optional[Tuple[str, str]],
            response_info: dict,
            raw_usage: Optional[dict],
            response_bytes: int,
            request_bytes: int,
        ) -> None:
            request_model = None
            request_stream = None
            if isinstance(request_payload, dict):
                request_model = request_payload.get("model") if isinstance(request_payload.get("model"), str) else None
                request_stream = request_payload.get("stream") if isinstance(request_payload.get("stream"), bool) else None

            parsed_path = urllib.parse.urlsplit(self.path)
            usage = normalize_usage(raw_usage)
            record = {
                "timestamp": now_iso(),
                "request_id": request_id,
                "client_ip": self.client_address[0],
                "method": self.command,
                "path": parsed_path.path,
                "status": status,
                "duration_ms": round((time.monotonic() - started_at) * 1000, 3),
                "request_bytes": request_bytes,
                "response_bytes": response_bytes,
                "request_model": request_model,
                "upstream_model": rewrite[1] if rewrite else request_model,
                "response_model": response_info.get("response_model"),
                "model_rewritten": bool(rewrite),
                "stream": bool(request_stream or response_info.get("observed_stream")),
                "response_id": response_info.get("response_id"),
                "response_status": response_info.get("response_status"),
                "incomplete_reason": response_info.get("incomplete_reason"),
                "upstream_request_id": response_info.get("upstream_request_id"),
                "error_type": response_info.get("response_error_type"),
                "error_code": response_info.get("response_error_code"),
                "error_message": response_info.get("response_error_message"),
                "usage": usage,
                "raw_usage": raw_usage or {},
            }
            if response_info.get("usage_parse_warning"):
                record["usage_parse_warning"] = response_info["usage_parse_warning"]
            try:
                write_usage_log(config, {key: value for key, value in record.items() if value is not None})
            except Exception as exc:
                self.log_error("failed to write usage log: %r", exc)

        def respond_proxy_error(self, exc: Exception) -> None:
            self.respond_json_error(502, f"proxy upstream request failed: {exc}", "proxy_error")

        def respond_json_error(self, status: int, message: str, error_type: str) -> None:
            payload = json.dumps(
                {"error": {"message": message, "type": error_type}},
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)

    return OpenAIProxyHandler


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal OpenAI API proxy with model-name rewriting.")
    parser.add_argument("--config", help="JSON config path. Defaults to ./config.json when it exists.")
    parser.add_argument("--host", help="Listen host. Defaults to config/env or 0.0.0.0.")
    parser.add_argument("--port", type=int, help="Listen port. Defaults to config/env or 8000.")
    parser.add_argument("--target-base-url", help="Upstream OpenAI-compatible endpoint base URL.")
    parser.add_argument("--model-map", help='JSON object, for example {"gpt-5.5":"custom-model"}.')
    parser.add_argument("--timeout", type=float, help="Upstream timeout in seconds.")
    parser.add_argument(
        "--strip-prefix",
        help='Incoming path prefix to strip before joining target URL. Use "" to disable. Defaults to auto.',
    )
    parser.add_argument(
        "--usage-log",
        help='Usage JSONL log path. Defaults to config/env or usage.jsonl. Use "" or "none" to disable.',
    )
    parser.add_argument(
        "--usage-capture-max-bytes",
        type=int,
        help="Maximum non-stream response bytes to hold for usage parsing. Defaults to 1048576.",
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
            f"minimal-openai-proxy listening on http://{config.host}:{config.port}; "
            "upstream target configured\n"
        )
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
