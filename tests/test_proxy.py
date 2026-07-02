import json
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

from minimal_openai_proxy import ProxyConfig, ThreadingHTTPServer, make_handler


class Capture:
    requests = []


class FakeUpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        request_payload = json.loads(body.decode("utf-8")) if body else {}
        Capture.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "accept_encoding": self.headers.get("Accept-Encoding"),
                "body": body,
            }
        )
        usage = {
            "input_tokens": 10,
            "input_tokens_details": {"cached_tokens": 3, "cache_write_tokens": 2},
            "output_tokens": 5,
            "output_tokens_details": {"reasoning_tokens": 2},
            "total_tokens": 15,
        }
        response_payload = {
            "id": "resp_test",
            "object": "response",
            "status": "completed",
            "model": request_payload.get("model"),
            "usage": usage,
        }
        if request_payload.get("stream") is True and request_payload.get("force_json") is not True:
            stream_payload = {
                "type": "response.completed",
                "response": response_payload,
                "sequence_number": 1,
            }
            payload = f"event: response.completed\ndata: {json.dumps(stream_payload)}\n\n".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            midpoint = len(payload) // 2
            self.wfile.write(payload[:midpoint])
            self.wfile.flush()
            self.wfile.write(payload[midpoint:])
            return

        payload = json.dumps(response_payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if request_payload.get("force_identity") is True:
            self.send_header("Content-Encoding", "identity")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        Capture.requests.append({"method": self.command, "path": self.path})
        payload = json.dumps({"models": []}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def start_server(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


class ProxyTest(unittest.TestCase):
    def setUp(self):
        Capture.requests = []
        self.tempdir = tempfile.TemporaryDirectory()
        self.usage_log_path = f"{self.tempdir.name}/usage.jsonl"
        self.upstream = HTTPServer(("127.0.0.1", 0), FakeUpstreamHandler)
        start_server(self.upstream)
        upstream_port = self.upstream.server_address[1]

        config = ProxyConfig(
            target_base_url=f"http://127.0.0.1:{upstream_port}/api/openai/v1",
            model_map={
                "gpt-5.5": "gpt-5.5-0424-global",
                "gpt-5.4": "gpt-5.4-0305-global",
                "gpt-5.3": "gpt-5.3-chat-0303-global",
            },
            usage_log_path=self.usage_log_path,
        )
        self.proxy = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(config))
        start_server(self.proxy)
        self.proxy_port = self.proxy.server_address[1]

    def tearDown(self):
        self.proxy.shutdown()
        self.proxy.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.tempdir.cleanup()

    def proxy_url(self, path):
        return f"http://127.0.0.1:{self.proxy_port}{path}"

    def read_usage_records(self):
        with open(self.usage_log_path, "r", encoding="utf-8") as file:
            return [json.loads(line) for line in file if line.strip()]

    def test_health_check_does_not_expose_upstream_config(self):
        with urllib.request.urlopen(self.proxy_url("/healthz"), timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {"ok": True})

    def test_rewrites_response_api_model_and_forwards_authorization(self):
        body = json.dumps({"model": "gpt-5.5", "input": "hello"}).encode("utf-8")
        request = urllib.request.Request(
            self.proxy_url("/v1/responses"),
            data=body,
            headers={
                "Authorization": "Bearer test-key",
                "Accept-Encoding": "gzip",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            response_payload = json.loads(response.read())
            self.assertEqual(response_payload["model"], "gpt-5.5-0424-global")

        self.assertEqual(len(Capture.requests), 1)
        captured = Capture.requests[0]
        self.assertEqual(captured["path"], "/api/openai/v1/responses")
        self.assertEqual(captured["authorization"], "Bearer test-key")
        self.assertEqual(captured["accept_encoding"], "identity")
        self.assertEqual(
            json.loads(captured["body"]),
            {"model": "gpt-5.5-0424-global", "input": "hello"},
        )

        usage_record = self.read_usage_records()[-1]
        self.assertEqual(usage_record["request_model"], "gpt-5.5")
        self.assertEqual(usage_record["upstream_model"], "gpt-5.5-0424-global")
        self.assertEqual(usage_record["response_model"], "gpt-5.5-0424-global")
        self.assertEqual(usage_record["usage"]["input_tokens"], 10)
        self.assertEqual(usage_record["usage"]["output_tokens"], 5)
        self.assertEqual(usage_record["usage"]["input_cached_read_tokens"], 3)
        self.assertEqual(usage_record["usage"]["input_cached_write_tokens"], 2)
        self.assertEqual(usage_record["usage"]["output_reasoning_tokens"], 2)
        self.assertNotIn("authorization", json.dumps(usage_record).lower())

    def test_leaves_unknown_model_unchanged(self):
        body = json.dumps({"model": "custom", "input": "hello"}).encode("utf-8")
        request = urllib.request.Request(
            self.proxy_url("/v1/responses"),
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)

        self.assertEqual(json.loads(Capture.requests[0]["body"])["model"], "custom")

    def test_drops_encrypted_reasoning_input_items_before_forwarding(self):
        body = json.dumps(
            {
                "model": "gpt-5.5",
                "input": [
                    {
                        "type": "reasoning",
                        "summary": [],
                        "content": None,
                        "encrypted_content": "gAAAA_invalid_for_upstream",
                    },
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hello"}],
                    },
                ],
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self.proxy_url("/v1/responses"),
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)

        forwarded = json.loads(Capture.requests[0]["body"])
        self.assertEqual(forwarded["model"], "gpt-5.5-0424-global")
        self.assertEqual(
            forwarded["input"],
            [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                }
            ],
        )

    def test_drops_encrypted_reasoning_even_without_model_rewrite(self):
        body = json.dumps(
            {
                "model": "custom",
                "input": [
                    {
                        "type": "reasoning",
                        "summary": [],
                        "content": None,
                        "encrypted_content": "gAAAA_invalid_for_upstream",
                    },
                    "hello",
                ],
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self.proxy_url("/v1/responses"),
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)

        forwarded = json.loads(Capture.requests[0]["body"])
        self.assertEqual(forwarded["model"], "custom")
        self.assertEqual(forwarded["input"], ["hello"])

    def test_get_is_forwarded_with_query_string(self):
        with urllib.request.urlopen(self.proxy_url("/v1/models?limit=1"), timeout=5) as response:
            self.assertEqual(response.status, 200)

        self.assertEqual(Capture.requests[0]["method"], "GET")
        self.assertEqual(Capture.requests[0]["path"], "/api/openai/v1/models?limit=1")

    def test_streaming_response_usage_is_logged_from_sse(self):
        body = json.dumps({"model": "gpt-5.4", "input": "hello", "stream": True}).encode("utf-8")
        request = urllib.request.Request(
            self.proxy_url("/v1/responses"),
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"response.completed", response.read())

        usage_record = self.read_usage_records()[-1]
        self.assertEqual(usage_record["request_model"], "gpt-5.4")
        self.assertEqual(usage_record["upstream_model"], "gpt-5.4-0305-global")
        self.assertEqual(usage_record["response_model"], "gpt-5.4-0305-global")
        self.assertTrue(usage_record["stream"])
        self.assertEqual(usage_record["usage"]["total_tokens"], 15)

    def test_stream_true_json_response_still_logs_usage(self):
        body = json.dumps(
            {"model": "gpt-5.3", "input": "hello", "stream": True, "force_json": True}
        ).encode("utf-8")
        request = urllib.request.Request(
            self.proxy_url("/v1/responses"),
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            payload = json.loads(response.read())
            self.assertEqual(payload["model"], "gpt-5.3-chat-0303-global")

        usage_record = self.read_usage_records()[-1]
        self.assertEqual(usage_record["response_model"], "gpt-5.3-chat-0303-global")
        self.assertTrue(usage_record["stream"])
        self.assertEqual(usage_record["usage"]["total_tokens"], 15)

    def test_identity_encoded_json_response_still_logs_usage(self):
        body = json.dumps({"model": "gpt-5.5", "input": "hello", "force_identity": True}).encode("utf-8")
        request = urllib.request.Request(
            self.proxy_url("/v1/responses"),
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            response.read()

        usage_record = self.read_usage_records()[-1]
        self.assertEqual(usage_record["response_model"], "gpt-5.5-0424-global")
        self.assertEqual(usage_record["usage"]["total_tokens"], 15)
        self.assertNotIn("usage_parse_warning", usage_record)

    def test_forwards_large_request_body_to_upstream(self):
        body = json.dumps({"model": "gpt-5.5", "input": "x" * 2000}).encode("utf-8")
        request = urllib.request.Request(
            self.proxy_url("/v1/responses"),
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            response.read()

        self.assertEqual(len(Capture.requests), 1)
        usage_record = self.read_usage_records()[-1]
        self.assertEqual(usage_record["status"], 200)
        self.assertGreater(usage_record["request_bytes"], 2000)

    def test_rejects_chunked_request_body_instead_of_dropping_it(self):
        with socket.create_connection(("127.0.0.1", self.proxy_port), timeout=5) as client:
            client.sendall(
                b"POST /v1/responses HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"Connection: close\r\n"
                b"\r\n"
                b"20\r\n"
                b'{"model":"gpt-5.5","input":"x"}'
                b"\r\n0\r\n\r\n"
            )
            chunks = []
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            response = b"".join(chunks)

        self.assertIn(b" 501 ", response.split(b"\r\n", 1)[0])
        self.assertIn(b"unsupported_request_body", response)
        self.assertEqual(Capture.requests, [])


if __name__ == "__main__":
    unittest.main()
