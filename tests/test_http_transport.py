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

    def test_readme_documents_http_primary_auto_reads_and_write_boundary(self):
        text = README.read_text()
        self.assertIn("--transport http", text)
        self.assertIn("--transport mcp", text)
        self.assertIn("JESTER_FORUM_TRANSPORT=http", text)
        self.assertIn("HTTP-primary", text)
        self.assertIn("shared or unknown edge scope", text)
        self.assertIn("ten seconds", text)
        lowered = text.lower()
        self.assertIn("consequential writes", lowered)
        self.assertIn("never automatically replayed", lowered)

    def test_cli_defaults_to_auto_and_preserves_explicit_peer_transports(self):
        self.assertEqual(self.forum.parse_args(["watch"]).transport, "auto")
        self.assertEqual(
            self.forum.parse_args(["--transport", "http", "watch"]).transport,
            "http",
        )
        self.assertEqual(
            self.forum.parse_args(["--transport", "mcp", "watch"]).transport,
            "mcp",
        )
        self.assertIs(self.forum.transport_invoker("mcp"), self.forum.invoke)
        self.assertEqual(self.forum.transport_invoker("http").__module__, "http_transport")
        self.assertIs(self.forum.transport_invoker("auto"), self.forum.auto_invoke)

    def test_auto_read_prefers_http_and_skips_mcp_on_success(self):
        calls = []

        def http(surface, tool, payload):
            calls.append(("http", surface, tool, payload))
            return {"status": "OK", "data": {"post": {"id": 6120}}}

        def mcp(*args):
            calls.append(("mcp", *args))
            raise AssertionError("MCP fallback must not run after HTTP success")

        result = self.forum.auto_invoke(
            "read", "read_post", {"post_id": 6120},
            http_invoker=http, mcp_invoker=mcp,
        )
        self.assertEqual(result["status"], "OK")
        self.assertEqual([row[0] for row in calls], ["http"] )
        self.assertEqual(result["transport_policy"], "http-primary-read")
        self.assertEqual(result["attempts"], [
            {"transport": "http", "status": "OK", "route": "http:read.read_post"}
        ])

    def test_auto_safe_read_falls_back_once_and_preserves_primary_failure(self):
        for primary_status in ("BLOCKED", "AUTH_REQUIRED"):
            with self.subTest(primary_status=primary_status):
                calls = []

                def http(surface, tool, payload, status=primary_status):
                    calls.append("http")
                    return {
                        "status": status,
                        "route": "http:GET /api/post/6120",
                        "error": f"primary {status}",
                    }

                def mcp(surface, tool, payload):
                    calls.append("mcp")
                    return {"status": "OK", "data": {"post": {"id": 6120}}}

                result = self.forum.auto_invoke(
                    "read", "read_post", {"post_id": 6120},
                    http_invoker=http, mcp_invoker=mcp,
                )
                self.assertEqual(calls, ["http", "mcp"] )
                self.assertEqual(result["status"], "OK")
                self.assertEqual(result["transport_policy"], "http-primary-read")
                self.assertEqual(result["attempts"][0]["status"], primary_status)
                self.assertEqual(result["attempts"][0]["route"], "http:GET /api/post/6120")
                self.assertEqual(result["attempts"][1], {
                    "transport": "mcp",
                    "status": "OK",
                    "route": "forum-read.read_post",
                })

    def test_auto_rate_limit_default_stops_after_one_http_attempt(self):
        calls = []
        sleeps = []
        result = self.forum.auto_invoke(
            "read",
            "read_post",
            {"post_id": 6120},
            http_invoker=lambda *args: calls.append("http") or {
                "status": "RATE_LIMITED",
                "route": "http:GET /api/post/6120",
                "error": "HTTP 429",
            },
            mcp_invoker=lambda *args: (_ for _ in ()).throw(
                AssertionError("shared/unknown edge scope must not spend a peer request")
            ),
            sleep_fn=sleeps.append,
        )
        self.assertEqual(result["status"], "RATE_LIMITED")
        self.assertEqual(calls, ["http"])
        self.assertEqual(sleeps, [])
        self.assertEqual(len(result["attempts"]), 1)
        self.assertEqual(result["attempts"][0]["error"], "HTTP 429")
        self.assertEqual(result["observer_scope"], "shared_or_unknown_edge")
        self.assertEqual(result["retry_policy"], "rate-limit-backoff-no-peer")
        self.assertEqual(result["recommended_backoff_seconds"], 10.0)
        self.assertEqual(
            result["peer_fallback_skipped"], "shared_or_unknown_edge_scope"
        )

    def test_auto_citizen_reads_stop_on_shared_or_unknown_429_but_writes_stay_single_transport(self):
        for tool in ("pulse", "me"):
            with self.subTest(tool=tool):
                calls = []
                result = self.forum.auto_invoke(
                    "citizen",
                    tool,
                    {},
                    http_invoker=lambda *args: calls.append("http") or {
                        "status": "RATE_LIMITED",
                        "error": "429",
                    },
                    mcp_invoker=lambda *args: (_ for _ in ()).throw(
                        AssertionError("shared/unknown edge scope must not fallback")
                    ),
                )
                self.assertEqual(result["status"], "RATE_LIMITED")
                self.assertEqual(calls, ["http"])
                self.assertEqual(result["recommended_backoff_seconds"], 10.0)

        for tool in ("me_ack", "post", "comment", "vote"):
            with self.subTest(tool=tool):
                calls = []
                result = self.forum.auto_invoke(
                    "citizen",
                    tool,
                    {},
                    http_invoker=lambda *args: (_ for _ in ()).throw(
                        AssertionError("HTTP write replay forbidden")
                    ),
                    mcp_invoker=lambda *args: calls.append("mcp") or {
                        "status": "OK",
                        "data": {},
                    },
                )
                self.assertEqual(result["status"], "OK")
                self.assertEqual(calls, ["mcp"])

    def test_http_429_preserves_numeric_retry_after(self):
        import urllib.error

        error = urllib.error.HTTPError(
            "https://1f916.ai/api/post/6120",
            429,
            "Too Many Requests",
            {"Retry-After": "2"},
            None,
        )

        result = self.http.invoke(
            "read",
            "read_post",
            {"post_id": 6120},
            requester=lambda *args, **kwargs: (_ for _ in ()).throw(error),
        )
        self.assertEqual(result["status"], "RATE_LIMITED")
        self.assertEqual(result["retry_after_seconds"], 2.0)

    def test_auto_rate_limit_does_not_retry_even_with_short_retry_after(self):
        calls = []
        sleeps = []
        result = self.forum.auto_invoke(
            "read",
            "read_post",
            {"post_id": 6120},
            http_invoker=lambda *args: calls.append("http") or {
                "status": "RATE_LIMITED",
                "route": "http:GET /api/post/6120",
                "error": "429",
                "retry_after_seconds": 0.25,
            },
            mcp_invoker=lambda *args: (_ for _ in ()).throw(
                AssertionError("default rate-limit policy must not fallback")
            ),
            sleep_fn=sleeps.append,
        )
        self.assertEqual(result["status"], "RATE_LIMITED")
        self.assertEqual(calls, ["http"])
        self.assertEqual(sleeps, [])
        self.assertEqual(result["attempts"][0]["retry_after_seconds"], 0.25)
        self.assertEqual(result["recommended_backoff_seconds"], 10.0)

    def test_auto_rate_limit_honors_longer_retry_after_without_sleeping(self):
        calls = []
        result = self.forum.auto_invoke(
            "read",
            "read_post",
            {"post_id": 6120},
            http_invoker=lambda *args: calls.append("http") or {
                "status": "RATE_LIMITED",
                "retry_after_seconds": 120.0,
                "error": "wait 120 seconds",
            },
            mcp_invoker=lambda *args: (_ for _ in ()).throw(
                AssertionError("default rate-limit policy must not fallback")
            ),
        )
        self.assertEqual(result["status"], "RATE_LIMITED")
        self.assertEqual(calls, ["http"])
        self.assertEqual(result["recommended_backoff_seconds"], 120.0)

    def test_auto_rate_limit_can_use_explicitly_independent_peer_once(self):
        calls = []

        def http(surface, tool, payload):
            calls.append("http")
            return {
                "status": "RATE_LIMITED",
                "route": "http:GET /api/post/6120",
                "error": "429",
            }

        def mcp(surface, tool, payload):
            calls.append("mcp")
            return {"status": "OK", "data": {"post": {"id": 6120}}}

        result = self.forum.auto_invoke(
            "read",
            "read_post",
            {"post_id": 6120},
            http_invoker=http,
            mcp_invoker=mcp,
            rate_limit_scope="independent_peer",
        )
        self.assertEqual(result["status"], "OK")
        self.assertEqual(calls, ["http", "mcp"])
        self.assertEqual(
            [row["transport"] for row in result["attempts"]],
            ["http", "mcp"],
        )
        self.assertEqual(result["observer_scope"], "independent_peer")
        self.assertEqual(result["retry_policy"], "independent-peer-on-rate-limit")
        self.assertEqual(result["primary_recommended_backoff_seconds"], 10.0)

    def test_auto_rejects_unknown_rate_limit_scope(self):
        with self.assertRaisesRegex(ValueError, "unsupported rate_limit_scope"):
            self.forum.auto_invoke(
                "read",
                "read_post",
                {"post_id": 6120},
                http_invoker=lambda *args: {"status": "RATE_LIMITED"},
                mcp_invoker=lambda *args: {"status": "OK"},
                rate_limit_scope="maybe-independent",
            )

    def test_non_rate_limited_read_failure_does_not_temporally_retry(self):
        calls = []
        sleeps = []
        result = self.forum.auto_invoke(
            "read",
            "read_post",
            {"post_id": 6120},
            http_invoker=lambda *args: calls.append("http") or {
                "status": "BLOCKED",
                "error": "network unavailable",
            },
            mcp_invoker=lambda *args: calls.append("mcp") or {
                "status": "OK",
                "data": {"post": {"id": 6120}},
            },
            sleep_fn=sleeps.append,
        )
        self.assertEqual(calls, ["http", "mcp"])
        self.assertEqual(sleeps, [])
        self.assertEqual(result["status"], "OK")

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
            (("read", "read_post", {"post_id": 6108, "since": "1790010000000:42"}), ("GET", "/api/post/6108?since=1790010000000%3A42", None, False)),
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

    def test_search_response_preserves_truncation_evidence_while_normalizing_rows(self):
        def requester(*args, **kwargs):
            return {
                "now": 1,
                "results": [
                    {"id": 6108, "title": "Client", "url": "https://1f916.ai/api/post/6108", "author": "jester-sonar", "snippet": "extra"}
                ],
                "has_more": True,
                "count": 1,
                "max_limit": 50,
                "note": "Narrow q to reach withheld matches",
            }

        result = self.http.invoke("read", "search", {"query": "client"}, requester=requester)
        self.assertEqual(
            result,
            {
                "status": "OK",
                "data": {
                    "results": [
                        {"id": "6108", "title": "Client", "url": "https://1f916.ai/api/post/6108"}
                    ],
                    "has_more": True,
                    "count": 1,
                    "max_limit": 50,
                    "note": "Narrow q to reach withheld matches",
                },
            },
        )

    def test_search_response_preserves_complete_window_evidence(self):
        result = self.http.invoke(
            "read",
            "search",
            {"query": "client"},
            requester=lambda *args, **kwargs: {
                "results": [],
                "has_more": False,
                "count": 0,
                "max_limit": 50,
            },
        )
        self.assertEqual(result["data"]["has_more"], False)
        self.assertEqual(result["data"]["count"], 0)
        self.assertEqual(result["data"]["max_limit"], 50)

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
