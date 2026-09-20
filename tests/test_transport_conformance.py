import io
import json
import pathlib
import tempfile
import types
import unittest
import urllib.error

import forum
import http_transport
import forum_ledger
import forum_state


def mcp_runner_ok(data):
    def runner(*args, **kwargs):
        return types.SimpleNamespace(
            returncode=0,
            stdout=json.dumps(data),
            stderr="",
        )
    return runner


def mcp_runner_error(message):
    def runner(*args, **kwargs):
        return types.SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=message,
        )
    return runner


def http_error(code, body=None):
    return urllib.error.HTTPError(
        "https://1f916.ai/api/test",
        code,
        "error",
        {},
        io.BytesIO((body or f"HTTP {code}").encode()),
    )



class ScriptedHttp:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def requester(self, path, method="GET", payload=None, auth=False, headers=None):
        if not self.steps:
            raise AssertionError(f"unexpected HTTP call: {method} {path}")
        wanted_method, wanted_path, outcome = self.steps.pop(0)
        self.calls.append((method, path, payload, auth, headers or {}))
        if (method, path) != (wanted_method, wanted_path):
            raise AssertionError(
                f"HTTP fixture mismatch: wanted {(wanted_method, wanted_path)!r}, got {(method, path)!r}"
            )
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def invoker(self, surface, tool, payload):
        return http_transport.invoke(
            surface,
            tool,
            payload,
            requester=self.requester,
            cache_path=None,
        )


class ScriptedMcp:
    def __init__(self, steps, credential="demo-secret"):
        self.steps = list(steps)
        self.calls = []
        self.credential = credential

    def runner(self, argv, **kwargs):
        route = next(part for part in argv if part.startswith("forum-"))
        payload = json.loads(argv[argv.index("--args") + 1])
        env = kwargs.get("env") or {}
        self.calls.append((route, payload, env.get("JESTER_FORUM_CREDENTIAL")))
        if not self.steps:
            raise AssertionError(f"unexpected MCP call: {route}")
        wanted_route, outcome = self.steps.pop(0)
        if route != wanted_route:
            raise AssertionError(f"MCP fixture mismatch: wanted {wanted_route!r}, got {route!r}")
        if isinstance(outcome, BaseException):
            return types.SimpleNamespace(returncode=1, stdout="", stderr=str(outcome))
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(outcome), stderr="")

    def invoker(self, surface, tool, payload):
        return forum.invoke(
            surface,
            tool,
            payload,
            runner=self.runner,
            base_env={},
            load_value=lambda: self.credential,
        )

def read_projection(tool, result):
    if result.get("status") != "OK":
        return {"status": result.get("status")}
    data = result.get("data") or {}
    if tool == "read_post":
        row = data.get("post") or {}
        semantic = {
            "id": row.get("id"),
            "title": row.get("title"),
            "body": row.get("body"),
            "author": row.get("author"),
        }
    elif tool == "read_comment":
        row = data.get("comment") or {}
        semantic = {
            "id": row.get("id"),
            "post_id": row.get("post_id"),
            "parent_id": row.get("parent_id"),
            "body": row.get("body"),
            "author": row.get("author"),
        }
    elif tool == "search":
        semantic = [
            (str(row.get("id")), row.get("title"), row.get("url"))
            for row in data.get("results", [])
            if isinstance(row, dict)
        ]
    else:
        raise AssertionError(tool)
    return {"status": "OK", "semantic": semantic}


class CrossTransportAdapterConformanceTests(unittest.TestCase):
    def _mcp(self, tool, payload, data):
        return forum.invoke(
            "read",
            tool,
            payload,
            runner=mcp_runner_ok(data),
            base_env={},
        )

    def _http(self, tool, payload, data):
        return http_transport.invoke(
            "read",
            tool,
            payload,
            requester=lambda *args, **kwargs: data,
            cache_path=None,
        )

    def test_read_post_success_semantics_match_while_metadata_may_differ(self):
        semantic = {
            "id": 6108,
            "title": "Client bench",
            "body": "body",
            "author": "jester-sonar",
        }
        http_data = {"now": 123, "now_utc": "demo", "post": dict(semantic), "untrusted_content": True}
        mcp_data = {"post": dict(semantic), "comments": []}
        http = self._http("read_post", {"post_id": 6108}, http_data)
        mcp = self._mcp("read_post", {"post_id": 6108}, mcp_data)
        self.assertEqual(read_projection("read_post", http), read_projection("read_post", mcp))
        self.assertNotEqual(set(http["data"]), set(mcp["data"]))

    def test_read_comment_success_semantics_match(self):
        semantic = {
            "id": 71462,
            "post_id": 6108,
            "parent_id": None,
            "body": "release",
            "author": "jester-sonar",
        }
        http = self._http("read_comment", {"comment_id": 71462}, {"now": 1, "comment": dict(semantic)})
        mcp = self._mcp("read_comment", {"comment_id": 71462}, {"comment": dict(semantic)})
        self.assertEqual(read_projection("read_comment", http), read_projection("read_comment", mcp))

    def test_search_success_semantics_match_after_http_normalization(self):
        raw = {
            "now": 1,
            "results": [
                {"id": 6108, "title": "Client", "url": "https://1f916.ai/api/post/6108", "snippet": "extra"},
                {"id": 6120, "title": "Other", "url": "https://1f916.ai/api/post/6120", "author": "x"},
            ],
        }
        mcp_shape = {
            "results": [
                {"id": "6108", "title": "Client", "url": "https://1f916.ai/api/post/6108"},
                {"id": "6120", "title": "Other", "url": "https://1f916.ai/api/post/6120"},
            ]
        }
        http = self._http("search", {"query": "client"}, raw)
        mcp = self._mcp("search", {"query": "client"}, mcp_shape)
        self.assertEqual(read_projection("search", http), read_projection("search", mcp))

    def test_equivalent_auth_failures_share_task_status_but_keep_route_provenance(self):
        mcp = forum.invoke(
            "read",
            "read_post",
            {"post_id": 6108},
            runner=mcp_runner_error("401 unauthorized"),
            base_env={},
        )
        http = http_transport.invoke(
            "read",
            "read_post",
            {"post_id": 6108},
            requester=lambda *args, **kwargs: (_ for _ in ()).throw(http_error(401)),
            cache_path=None,
        )
        self.assertEqual(mcp["status"], "AUTH_REQUIRED")
        self.assertEqual(http["status"], "AUTH_REQUIRED")
        self.assertNotEqual(mcp["route"], http["route"])

    def test_equivalent_rate_limit_failures_share_task_status_but_keep_route_provenance(self):
        mcp = forum.invoke(
            "read", "read_post", {"post_id": 6108},
            runner=mcp_runner_error("429 rate limit"), base_env={},
        )
        http = http_transport.invoke(
            "read", "read_post", {"post_id": 6108},
            requester=lambda *args, **kwargs: (_ for _ in ()).throw(http_error(429)),
            cache_path=None,
        )
        self.assertEqual(mcp["status"], "RATE_LIMITED")
        self.assertEqual(http["status"], "RATE_LIMITED")
        self.assertNotEqual(mcp["route"], http["route"])

    def test_equivalent_generic_failures_share_blocked_status(self):
        mcp = forum.invoke(
            "read", "read_post", {"post_id": 6108},
            runner=mcp_runner_error("transport failed"), base_env={},
        )
        http = http_transport.invoke(
            "read", "read_post", {"post_id": 6108},
            requester=lambda *args, **kwargs: (_ for _ in ()).throw(urllib.error.URLError("transport failed")),
            cache_path=None,
        )
        self.assertEqual(mcp["status"], "BLOCKED")
        self.assertEqual(http["status"], "BLOCKED")
        self.assertNotEqual(mcp["route"], http["route"])


class CrossTransportWriteConformanceTests(unittest.TestCase):
    def _run_pair(self, argv, http_steps, mcp_steps, prepare_state=None):
        results = []
        for label, backend in (
            ("http", ScriptedHttp(http_steps)),
            ("mcp", ScriptedMcp(mcp_steps)),
        ):
            with tempfile.TemporaryDirectory() as td:
                state = pathlib.Path(td) / "state.json"
                operations = pathlib.Path(td) / "operations.json"
                if prepare_state is not None:
                    prepare_state(state)
                result = forum.execute(
                    forum.parse_args(argv),
                    invoker=backend.invoker,
                    state_path=state,
                    operations_path=operations,
                )
                ledger = forum_ledger.load_ledger(operations) if operations.exists() else None
                results.append((label, result, backend, state.exists(), ledger))
        return results

    def test_comment_exact_readback_is_write_verified_on_both_transports(self):
        comment = {"id": 4000, "post_id": 6108, "parent_id": None, "body": "hello"}
        pair = self._run_pair(
            ["comment", "--post", "6108", "--body", "hello"],
            [
                ("POST", "/api/comment", {"comment_id": 4000}),
                ("GET", "/api/comment/4000", {"comment": comment}),
            ],
            [
                ("forum-citizen.comment", {"comment_id": 4000}),
                ("forum-read.read_comment", {"comment": comment}),
            ],
        )
        projections = []
        for _label, result, _backend, _state_exists, ledger in pair:
            projections.append((result["status"], result["operation"], result["readback_id"]))
            self.assertEqual(ledger["operations"][0]["state"], "VERIFIED")
        self.assertEqual(projections[0], projections[1])
        self.assertEqual(projections[0], ("WRITE_VERIFIED", "comment", 4000))

    def test_comment_ambiguous_write_is_recoverable_once_on_both_transports(self):
        pair = self._run_pair(
            ["comment", "--post", "6108", "--body", "hello"],
            [("POST", "/api/comment", http_error(429))],
            [("forum-citizen.comment", RuntimeError("429 rate limit"))],
        )
        for label, result, backend, _state_exists, ledger in pair:
            self.assertEqual(result["status"], "RECOVERABLE", label)
            self.assertEqual(result["operation"], "comment")
            self.assertEqual(result["transport_status"], "RATE_LIMITED")
            self.assertEqual(len(backend.calls), 1)
            self.assertEqual(ledger["operations"][0]["state"], "RECOVERABLE")
            self.assertFalse(ledger["operations"][0]["auto_replay_allowed"])

    def test_post_readback_mismatch_is_recoverable_on_both_transports(self):
        bad_post = {"id": 3000, "title": "T", "body": "different"}
        pair = self._run_pair(
            ["post", "--title", "T", "--body", "B"],
            [
                ("POST", "/api/post", {"post_id": 3000}),
                ("GET", "/api/post/3000", {"post": bad_post}),
            ],
            [
                ("forum-citizen.post", {"post_id": 3000}),
                ("forum-read.read_post", {"post": bad_post}),
            ],
        )
        for label, result, backend, _state_exists, ledger in pair:
            self.assertEqual(result["status"], "RECOVERABLE", label)
            self.assertEqual(result["operation"], "post")
            self.assertEqual(len(backend.calls), 2)
            self.assertEqual(ledger["operations"][0]["state"], "RECOVERABLE")

    def test_vote_count_proof_is_write_verified_on_both_transports(self):
        pair = self._run_pair(
            ["vote", "comment", "24315"],
            [
                ("GET", "/api/comment/24315", {"comment": {"id": 24315, "votes": 3}}),
                ("POST", "/api/vote", {"ok": True}),
                ("GET", "/api/comment/24315", {"comment": {"id": 24315, "votes": 4}}),
            ],
            [
                ("forum-read.read_comment", {"comment": {"id": 24315, "votes": 3}}),
                ("forum-citizen.vote", {"ok": True}),
                ("forum-read.read_comment", {"comment": {"id": 24315, "votes": 4}}),
            ],
        )
        for label, result, backend, _state_exists, ledger in pair:
            self.assertEqual(result["status"], "WRITE_VERIFIED", label)
            self.assertEqual(result["operation"], "vote")
            self.assertEqual(len(backend.calls), 3)
            self.assertEqual(ledger["operations"][0]["state"], "VERIFIED")

    def test_ack_progress_proof_is_write_verified_on_both_transports(self):
        offered = {
            "version": 1,
            "timestamp": 100,
            "comments": 10,
            "mentions": 20,
            "seal": "seal-a",
        }

        def prepare(state):
            forum_state.bank_inbox_page(
                state,
                {"ack_cursor": offered, "since_last_visit": {}},
                now_ms=1_000,
            )

        me = {
            "since_last_visit": {
                "interval": {
                    "comments": {"after": 10},
                    "mentions": {"after": 20},
                }
            }
        }
        pair = self._run_pair(
            ["ack"],
            [
                ("POST", "/api/me/ack", {"ok": True}),
                ("GET", "/api/me?cursor_mode=id", me),
            ],
            [
                ("forum-citizen.me_ack", {"ok": True}),
                ("forum-citizen.me", me),
            ],
            prepare_state=prepare,
        )
        for label, result, backend, state_exists, ledger in pair:
            self.assertEqual(result["status"], "WRITE_VERIFIED", label)
            self.assertEqual(result["operation"], "ack")
            self.assertEqual(len(backend.calls), 2)
            self.assertFalse(state_exists)
            self.assertEqual(ledger["operations"][0]["state"], "VERIFIED")

    def test_secret_boundary_differs_by_adapter_but_never_leaks_into_result(self):
        secret = "DEMO-SECRET-MUST-NOT-LEAK"
        http = ScriptedHttp([("POST", "/api/comment", http_error(429))])
        mcp = ScriptedMcp(
            [("forum-citizen.comment", RuntimeError("429 rate limit"))],
            credential=secret,
        )
        for backend in (http, mcp):
            with tempfile.TemporaryDirectory() as td:
                result = forum.execute(
                    forum.parse_args(["comment", "--post", "6108", "--body", "hello"]),
                    invoker=backend.invoker,
                    operations_path=pathlib.Path(td) / "operations.json",
                )
                self.assertNotIn(secret, json.dumps(result))
        self.assertTrue(http.calls[0][3])
        self.assertEqual(mcp.calls[0][2], secret)


if __name__ == "__main__":
    unittest.main()
