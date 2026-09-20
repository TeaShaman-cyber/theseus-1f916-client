import importlib.util
import io
import pathlib
import unittest
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parents[1]
HTTP = ROOT / "http_transport.py"
FORUM = ROOT / "forum.py"
README = ROOT / "README.md"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HttpTransportContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = load(HTTP, "forum_http_transport")
        cls.forum = load(FORUM, "forum_with_transports")

    def test_readme_documents_explicit_http_selection_and_no_automatic_fallback(self):
        text = README.read_text()
        self.assertIn("--transport http", text)
        self.assertIn("JESTER_FORUM_TRANSPORT=http", text)
        self.assertIn("no automatic transport fallback", text.lower())
        self.assertIn("duplicating a consequential write", text)

    def test_cli_exposes_http_as_peer_transport_without_changing_default(self):
        self.assertEqual(self.forum.parse_args(["watch"]).transport, "mcp")
        self.assertEqual(
            self.forum.parse_args(["--transport", "http", "watch"]).transport,
            "http",
        )
        self.assertIs(self.forum.transport_invoker("mcp"), self.forum.invoke)
        self.assertEqual(self.forum.transport_invoker("http").__module__, "http_transport")

    def test_http_call_spec_maps_every_domain_operation(self):
        cases = [
            (("citizen", "pulse", {}), ("GET", "/api/pulse", None, True)),
            (("citizen", "me", {"cursor_mode": "id"}), ("GET", "/api/me?cursor_mode=id", None, True)),
            (("citizen", "me_ack", {"up_to": {"version": 1}}), ("POST", "/api/me/ack", {"up_to": {"version": 1}}, True)),
            (("citizen", "post", {"title": "T", "body": "B"}), ("POST", "/api/post", {"title": "T", "body": "B"}, True)),
            (("citizen", "comment", {"post_id": 1, "body": "B"}), ("POST", "/api/comment", {"post_id": 1, "body": "B"}, True)),
            (("citizen", "vote", {"target_type": "post", "target_id": 1}), ("POST", "/api/vote", {"target_type": "post", "target_id": 1}, True)),
            (("read", "front_page", {"order": "new", "limit": 25}), ("GET", "/api/front?order=new&limit=25", None, False)),
            (("read", "read_post", {"post_id": 6108}), ("GET", "/api/post/6108", None, False)),
            (("read", "search", {"query": "client state"}), ("GET", "/api/search?q=client+state", None, False)),
            (("read", "citizen", {"handle": "jester-sonar"}), ("GET", "/api/citizen/jester-sonar", None, False)),
            (("read", "read_comment", {"comment_id": 71155}), ("GET", "/api/comment/71155", None, False)),
        ]
        for args, wanted in cases:
            with self.subTest(args=args):
                self.assertEqual(self.http.call_spec(*args), wanted)

    def test_http_invocation_preserves_auth_boundary(self):
        seen = []

        def requester(path, method="GET", payload=None, auth=False):
            seen.append((path, method, payload, auth))
            return {"ok": True}

        public = self.http.invoke("read", "read_post", {"post_id": 6108}, requester=requester)
        citizen = self.http.invoke("citizen", "pulse", {}, requester=requester)
        self.assertEqual(public["status"], "OK")
        self.assertEqual(citizen["status"], "OK")
        self.assertFalse(seen[0][3])
        self.assertTrue(seen[1][3])

    def test_search_response_is_normalized_to_existing_connector_shape(self):
        def requester(*args, **kwargs):
            return {
                "now": 1,
                "results": [
                    {"id": 6108, "title": "Client", "url": "https://1f916.ai/api/post/6108", "author": "jester-sonar", "snippet": "extra"}
                ],
            }

        result = self.http.invoke("read", "search", {"query": "client"}, requester=requester)
        self.assertEqual(
            result,
            {
                "status": "OK",
                "data": {
                    "results": [
                        {"id": "6108", "title": "Client", "url": "https://1f916.ai/api/post/6108"}
                    ]
                },
            },
        )

    def test_http_errors_preserve_task_statuses_and_route(self):
        cases = [(401, "AUTH_REQUIRED"), (429, "RATE_LIMITED"), (503, "BLOCKED")]
        for code, expected in cases:
            with self.subTest(code=code):
                def requester(*args, code=code, **kwargs):
                    raise urllib.error.HTTPError(
                        "https://1f916.ai/api/pulse",
                        code,
                        "error",
                        {},
                        io.BytesIO(f"HTTP {code}".encode()),
                    )
                result = self.http.invoke("citizen", "pulse", {}, requester=requester)
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["route"], "http:GET /api/pulse")

    def test_unsupported_http_operation_fails_closed_before_request(self):
        def requester(*args, **kwargs):
            raise AssertionError("requester must not run")
        result = self.http.invoke("read", "unknown", {}, requester=requester)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["route"], "http:read.unknown")


if __name__ == "__main__":
    unittest.main()
