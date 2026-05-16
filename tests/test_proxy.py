import json
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
        Capture.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": body,
            }
        )
        payload = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
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
        )
        self.proxy = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(config))
        start_server(self.proxy)
        self.proxy_port = self.proxy.server_address[1]

    def tearDown(self):
        self.proxy.shutdown()
        self.proxy.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()

    def proxy_url(self, path):
        return f"http://127.0.0.1:{self.proxy_port}{path}"

    def test_rewrites_response_api_model_and_forwards_authorization(self):
        body = json.dumps({"model": "gpt-5.5", "input": "hello"}).encode("utf-8")
        request = urllib.request.Request(
            self.proxy_url("/v1/responses"),
            data=body,
            headers={
                "Authorization": "Bearer test-key",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {"ok": True})

        self.assertEqual(len(Capture.requests), 1)
        captured = Capture.requests[0]
        self.assertEqual(captured["path"], "/api/openai/v1/responses")
        self.assertEqual(captured["authorization"], "Bearer test-key")
        self.assertEqual(
            json.loads(captured["body"]),
            {"model": "gpt-5.5-0424-global", "input": "hello"},
        )

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

    def test_get_is_forwarded_with_query_string(self):
        with urllib.request.urlopen(self.proxy_url("/v1/models?limit=1"), timeout=5) as response:
            self.assertEqual(response.status, 200)

        self.assertEqual(Capture.requests[0]["method"], "GET")
        self.assertEqual(Capture.requests[0]["path"], "/api/openai/v1/models?limit=1")


if __name__ == "__main__":
    unittest.main()
