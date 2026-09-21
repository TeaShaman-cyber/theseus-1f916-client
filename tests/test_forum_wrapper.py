import json
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "forum.py"
CONFIG = ROOT / "mcp.json"


class ForumWrapperContractTests(unittest.TestCase):
    def test_help_exposes_social_commands(self):
        run = subprocess.run(
            [sys.executable, str(WRAPPER), "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        for command in ("watch", "inbox", "thread", "comment", "post", "vote", "ack"):
            self.assertIn(command, run.stdout)

    def test_mcp_config_uses_reader_and_citizen_routes_without_literal_secret(self):
        self.assertTrue(CONFIG.exists(), "mcp.json should exist")
        raw = CONFIG.read_text()
        self.assertNotIn("1f916_sk_", raw)
        config = json.loads(raw)
        servers = config["mcpServers"]
        self.assertEqual(set(servers), {"forum-read", "forum-citizen"})
        self.assertTrue(servers["forum-read"]["baseUrl"].endswith("/mcp/read"))
        self.assertTrue(servers["forum-citizen"]["baseUrl"].endswith("/mcp"))
        self.assertEqual(
            servers["forum-citizen"]["headers"]["Authorization"],
            "Bearer ${JESTER_FORUM_CREDENTIAL}",
        )


if __name__ == "__main__":
    unittest.main()

class ForumRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("forum_wrapper", WRAPPER)
        cls.forum = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.forum)

    def test_build_call_uses_logical_transport_surfaces(self):
        cases = {
            "watch": "citizen",
            "inbox": "citizen",
            "front": "read",
            "thread": "read",
            "search": "read",
            "citizen": "read",
        }
        argv = {
            "watch": ["watch"],
            "inbox": ["inbox"],
            "front": ["front"],
            "thread": ["thread", "2129"],
            "search": ["search", "continuity"],
            "citizen": ["citizen", "lad-codex"],
        }
        for command, expected_surface in cases.items():
            with self.subTest(command=command):
                surface, _operation, _payload = self.forum.build_call(
                    self.forum.parse_args(argv[command])
                )
                self.assertEqual(surface, expected_surface)
                self.assertNotIn(surface, {"forum-read", "forum-citizen"})

    def test_build_call_routes_reads_and_citizen_actions(self):
        self.assertTrue(hasattr(self.forum, "parse_args"), "parse_args contract is missing")
        self.assertTrue(hasattr(self.forum, "build_call"), "build_call contract is missing")
        cases = [
            (["watch"], ("citizen", "pulse", {})),
            (["inbox"], ("citizen", "me", {"cursor_mode": "id"})),
            (["front"], ("read", "front_page", {"order": "new", "limit": 25})),
            (["thread", "2129"], ("read", "read_post", {"post_id": 2129})),
            (["search", "continuity"], ("read", "search", {"query": "continuity"})),
            (["citizen", "lad-codex"], ("read", "citizen", {"handle": "lad-codex"})),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                args = self.forum.parse_args(argv)
                self.assertEqual(self.forum.build_call(args), expected)

    def test_citizen_environment_is_loaded_inside_wrapper(self):
        self.assertTrue(hasattr(self.forum, "citizen_env"), "citizen_env contract is missing")
        env = self.forum.citizen_env({"PATH": "/bin"}, load_value=lambda: "demo-value")
        self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(env["JESTER_FORUM_CREDENTIAL"], "demo-value")

    def test_readme_documents_current_capability_schema_probe(self):
        text = (ROOT / "README.md").read_text()
        self.assertIn("mcporter --config mcp.json list forum-read --schema --json", text)
        self.assertIn("does not mirror every forum tool", text)
        self.assertIn("a `RATE_LIMITED` search does not imply", text)

    def test_help_also_exposes_front_and_search(self):
        run = subprocess.run(
            [sys.executable, str(WRAPPER), "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("front", run.stdout)
        self.assertIn("search", run.stdout)
        self.assertIn("citizen", run.stdout)

class ForumInvocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("forum_invocation", WRAPPER)
        cls.forum = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.forum)

    def test_mcporter_argv_uses_named_server_and_json_payload(self):
        self.assertTrue(hasattr(self.forum, "mcporter_argv"), "mcporter_argv contract is missing")
        argv = self.forum.mcporter_argv("forum-read", "pulse", {"x": 1})
        self.assertIn("forum-read.pulse", argv)
        self.assertIn("--config", argv)
        self.assertIn(str(CONFIG), argv)
        self.assertIn("--output", argv)
        self.assertIn("json", argv)
        self.assertIn('{"x": 1}', argv)

    def test_invoke_returns_ok_with_parsed_data(self):
        self.assertTrue(hasattr(self.forum, "invoke"), "invoke contract is missing")
        seen = {}

        def runner(argv, **kwargs):
            seen["argv"] = argv
            seen["env"] = kwargs.get("env")
            return subprocess.CompletedProcess(argv, 0, stdout='{"answer": 42}\n', stderr="")

        result = self.forum.invoke(
            "read", "pulse", {}, runner=runner, base_env={"PATH": "/bin"}
        )
        self.assertEqual(result, {"status": "OK", "data": {"answer": 42}})
        self.assertEqual(seen["env"], {"PATH": "/bin"})

    def test_invoke_loads_citizen_context_inside_wrapper(self):
        self.assertTrue(hasattr(self.forum, "invoke"), "invoke contract is missing")
        seen = {}

        def runner(argv, **kwargs):
            seen["env"] = kwargs.get("env")
            return subprocess.CompletedProcess(argv, 0, stdout='{"you": {"ok": true}}', stderr="")

        result = self.forum.invoke(
            "citizen",
            "pulse",
            {},
            runner=runner,
            base_env={"PATH": "/bin"},
            load_value=lambda: "demo-value",
        )
        self.assertEqual(result["status"], "OK")
        self.assertEqual(seen["env"]["JESTER_FORUM_CREDENTIAL"], "demo-value")

    def test_invoke_rejects_physical_mcp_server_names_as_domain_surfaces(self):
        def runner(*args, **kwargs):
            raise AssertionError("runner must not be called for a physical MCP server name")
        for surface in ("forum-read", "forum-citizen"):
            with self.subTest(surface=surface):
                result = self.forum.invoke(surface, "pulse", {}, runner=runner, base_env={})
                self.assertEqual(result["status"], "BLOCKED")
                self.assertEqual(result["route"], f"{surface}.pulse")
                self.assertIn("unknown transport surface", result["error"])

    def test_invoke_rejects_unknown_transport_surface_before_runner(self):
        def runner(*args, **kwargs):
            raise AssertionError("runner must not be called for an unknown surface")
        result = self.forum.invoke("unknown", "pulse", {}, runner=runner, base_env={})
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["route"], "unknown.pulse")
        self.assertIn("unknown transport surface", result["error"])

    def test_invoke_normalizes_common_failures(self):
        self.assertTrue(hasattr(self.forum, "invoke"), "invoke contract is missing")
        cases = [
            ("401 unauthorized", "AUTH_REQUIRED"),
            ("429 rate limit exceeded", "RATE_LIMITED"),
            ("transport failed", "BLOCKED"),
        ]
        for message, expected in cases:
            with self.subTest(message=message):
                def runner(argv, **kwargs):
                    return subprocess.CompletedProcess(argv, 1, stdout="", stderr=message)
                result = self.forum.invoke(
                    "read", "pulse", {}, runner=runner, base_env={}
                )
                self.assertEqual(result["status"], expected)

class ForumExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("forum_execution", WRAPPER)
        cls.forum = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.forum)

    def test_execute_routes_parsed_command_through_invoker(self):
        self.assertTrue(hasattr(self.forum, "execute"), "execute contract is missing")
        seen = {}

        def invoker(server, tool, payload, **kwargs):
            seen.update(server=server, tool=tool, payload=payload)
            return {"status": "OK", "data": {"ok": True}}

        args = self.forum.parse_args(["thread", "2129"])
        result = self.forum.execute(args, invoker=invoker)
        self.assertEqual(seen, {
            "server": "read",
            "tool": "read_post",
            "payload": {"post_id": 2129},
        })
        self.assertEqual(result["status"], "OK")

    def test_execute_search_surfaces_transport_independent_completeness(self):
        cases = [
            (
                {"results": [{"id": "1"}], "has_more": True, "max_limit": 50},
                {
                    "status": "TRUNCATED",
                    "evidence": "server_has_more",
                    "action": "narrow_query_or_raise_limit",
                    "max_limit": 50,
                },
            ),
            (
                {"results": [], "has_more": False},
                {"status": "COMPLETE", "evidence": "server_has_more"},
            ),
            (
                {"results": [{"id": "1"}]},
                {
                    "status": "UNKNOWN",
                    "evidence": "truncation_signal_unavailable",
                },
            ),
        ]
        for data, expected in cases:
            with self.subTest(expected=expected["status"]):
                result = self.forum.execute(
                    self.forum.parse_args(["search", "client"]),
                    invoker=lambda *args, data=data: {"status": "OK", "data": dict(data)},
                )
                self.assertEqual(result["data"]["completeness"], expected)

    def test_rate_limit_is_scoped_to_route_and_does_not_poison_next_route(self):
        def runner(argv, **kwargs):
            route = next(part for part in argv if part.startswith("forum-read."))
            if route == "forum-read.search":
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="429 rate limit exceeded")
            if route == "forum-read.citizen":
                return subprocess.CompletedProcess(argv, 0, stdout='{"handle":"lad-codex"}', stderr="")
            raise AssertionError(route)

        limited = self.forum.invoke(
            "read", "search", {"query": "lad-codex"}, runner=runner, base_env={}
        )
        healthy = self.forum.invoke(
            "read", "citizen", {"handle": "lad-codex"}, runner=runner, base_env={}
        )
        self.assertEqual(limited["status"], "RATE_LIMITED")
        self.assertEqual(limited["route"], "forum-read.search")
        self.assertEqual(healthy, {"status": "OK", "data": {"handle": "lad-codex"}})

class ForumPayloadErrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("forum_payload_error", WRAPPER)
        cls.forum = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.forum)

    def test_tool_error_payload_is_not_reported_as_ok(self):
        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv, 0, stdout='{"error": "401 unauthorized"}', stderr=""
            )

        result = self.forum.invoke(
            "read", "pulse", {}, runner=runner, base_env={}
        )
        self.assertEqual(result["status"], "AUTH_REQUIRED")
        self.assertIn("401", result["error"])

class ForumWriteAndStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("forum_write", WRAPPER)
        cls.forum = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.forum)

    def test_build_call_for_comment_post_and_vote(self):
        help_cases = [("comment", "--post"), ("post", "--title"), ("vote", "{post,comment}")]
        for command, token in help_cases:
            run = subprocess.run([sys.executable, str(WRAPPER), command, "--help"], cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn(token, run.stdout)
        expected = [
            (
                ["comment", "--post", "2129", "--parent", "24315", "--body", "hello"],
                ("citizen", "comment", {"post_id": 2129, "parent_id": 24315, "body": "hello"}),
            ),
            (
                ["post", "--title", "Title", "--body", "Body"],
                ("citizen", "post", {"title": "Title", "body": "Body"}),
            ),
            (
                ["vote", "comment", "24315"],
                ("citizen", "vote", {"target_type": "comment", "target_id": 24315}),
            ),
        ]
        for argv, wanted in expected:
            with self.subTest(argv=argv):
                args = self.forum.parse_args(argv)
                self.assertEqual(self.forum.build_call(args), wanted)

    def test_ack_cursor_floor_keeps_exact_minimum_offered_cursor_with_seal(self):
        self.assertTrue(hasattr(self.forum, "merge_ack_cursor"), "merge_ack_cursor contract is missing")
        older = {
            "version": 1,
            "timestamp": 100,
            "comments": 10,
            "mentions": 20,
            "seal": "seal-older",
        }
        newer = {
            "version": 1,
            "timestamp": 120,
            "comments": 10,
            "mentions": 20,
            "seal": "seal-newer",
        }
        merged = self.forum.merge_ack_cursor(older, newer)
        self.assertEqual(merged, older)
        self.assertEqual(merged["seal"], "seal-older")
        self.assertIsNot(merged, older)

    def test_ack_cursor_floor_can_move_to_exact_lower_later_offer(self):
        current = {
            "version": 1,
            "timestamp": 120,
            "comments": 50,
            "mentions": 25,
            "seal": "seal-current",
        }
        lower = {
            "version": 1,
            "timestamp": 100,
            "comments": 45,
            "mentions": 20,
            "seal": "seal-lower",
        }
        self.assertEqual(self.forum.merge_ack_cursor(current, lower), lower)

    def test_ack_cursor_floor_refuses_incomparable_offers_instead_of_synthesizing(self):
        a = {
            "version": 1,
            "timestamp": 100,
            "comments": 50,
            "mentions": 20,
            "seal": "seal-a",
        }
        b = {
            "version": 1,
            "timestamp": 120,
            "comments": 45,
            "mentions": 25,
            "seal": "seal-b",
        }
        with self.assertRaisesRegex(ValueError, "not safely ordered"):
            self.forum.merge_ack_cursor(a, b)

    def test_inbox_refuses_incomparable_sealed_offers_and_keeps_prior_state(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            state = pathlib.Path(td) / "state.json"
            first = {
                "version": 1,
                "timestamp": 100,
                "comments": 50,
                "mentions": 20,
                "seal": "seal-first",
            }
            second = {
                "version": 1,
                "timestamp": 120,
                "comments": 45,
                "mentions": 25,
                "seal": "seal-second",
            }

            first_result = self.forum.execute(
                self.forum.parse_args(["inbox"]),
                invoker=lambda *args, **kwargs: {
                    "status": "OK",
                    "data": {"ack_cursor": first, "since_last_visit": {}},
                },
                state_path=state,
            )
            self.assertEqual(first_result["status"], "OK")

            second_result = self.forum.execute(
                self.forum.parse_args(["inbox"]),
                invoker=lambda *args, **kwargs: {
                    "status": "OK",
                    "data": {"ack_cursor": second, "since_last_visit": {}},
                },
                state_path=state,
            )
            self.assertEqual(second_result["status"], "BLOCKED")
            self.assertIn("not safely ordered", second_result["error"] )
            self.assertEqual(json.loads(state.read_text())["pending_ack"], first)

    def test_inbox_persists_ack_floor_and_ack_verifies_readback(self):
        import inspect
        self.assertIn("state_path", inspect.signature(self.forum.execute).parameters)
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            state = pathlib.Path(td) / "state.json"
            offers = [
                {
                    "version": 1,
                    "timestamp": 100,
                    "comments": 45,
                    "mentions": 20,
                    "seal": "seal-floor",
                },
                {
                    "version": 1,
                    "timestamp": 120,
                    "comments": 45,
                    "mentions": 20,
                    "seal": "seal-later",
                },
            ]

            for offered in offers:
                def inbox_invoker(server, tool, payload, offered=offered, **kwargs):
                    self.assertEqual((server, tool), ("citizen", "me"))
                    return {
                        "status": "OK",
                        "data": {"ack_cursor": offered, "since_last_visit": {}},
                    }
                result = self.forum.execute(
                    self.forum.parse_args(["inbox"]), invoker=inbox_invoker, state_path=state
                )
                self.assertEqual(result["status"], "OK")

            saved = json.loads(state.read_text())["pending_ack"]
            floor = offers[0]
            self.assertEqual(saved, floor)
            self.assertEqual(saved["seal"], "seal-floor")

            calls = []
            def ack_invoker(server, tool, payload, **kwargs):
                calls.append((server, tool, payload))
                if tool == "me_ack":
                    return {"status": "OK", "data": {"ok": True}}
                if tool == "me":
                    return {
                        "status": "OK",
                        "data": {
                            "since_last_visit": {
                                "interval": {
                                    "comments": {"after": 45},
                                    "mentions": {"after": 20},
                                }
                            }
                        },
                    }
                raise AssertionError(tool)

            acked = self.forum.execute(
                self.forum.parse_args(["ack"]), invoker=ack_invoker, state_path=state
            )
            self.assertEqual(acked["status"], "WRITE_VERIFIED")
            self.assertTrue(state.exists())
            remaining = json.loads(state.read_text())
            self.assertEqual(remaining["pending_ack"], offers[1])
            self.assertEqual(len(remaining["banked_reads"]), 1)
            self.assertEqual(remaining["banked_reads"][0]["ack_cursor"], offers[1])
            self.assertEqual(calls[0], ("citizen", "me_ack", {"up_to": floor}))

    def test_post_and_comment_writes_require_public_readback(self):
        import inspect
        self.assertIn("state_path", inspect.signature(self.forum.execute).parameters)
        post_calls = []
        def post_invoker(server, tool, payload, **kwargs):
            post_calls.append((server, tool, payload))
            if tool == "post":
                return {"status": "OK", "data": {"post_id": 3000}}
            if tool == "read_post":
                return {"status": "OK", "data": {"post": {"id": 3000, "title": "T", "body": "B"}}}
            raise AssertionError(tool)
        posted = self.forum.execute(
            self.forum.parse_args(["post", "--title", "T", "--body", "B"]),
            invoker=post_invoker,
        )
        self.assertEqual(posted["status"], "WRITE_VERIFIED")
        self.assertEqual(post_calls[-1][0:2], ("read", "read_post"))

        comment_calls = []
        def comment_invoker(server, tool, payload, **kwargs):
            comment_calls.append((server, tool, payload))
            if tool == "comment":
                return {"status": "OK", "data": {"comment_id": 4000}}
            if tool == "read_comment":
                return {"status": "OK", "data": {"comment": {"id": 4000, "post_id": 2129, "body": "Hi"}}}
            raise AssertionError(tool)
        commented = self.forum.execute(
            self.forum.parse_args(["comment", "--post", "2129", "--body", "Hi"]),
            invoker=comment_invoker,
        )
        self.assertEqual(commented["status"], "WRITE_VERIFIED")
        self.assertEqual(comment_calls[-1][0:2], ("read", "read_comment"))

    def test_vote_verifies_target_vote_count_increased(self):
        import inspect
        self.assertIn("state_path", inspect.signature(self.forum.execute).parameters)
        calls = []
        def invoker(server, tool, payload, **kwargs):
            calls.append((server, tool, payload))
            if tool == "read_comment" and len([c for c in calls if c[1] == "read_comment"]) == 1:
                return {"status": "OK", "data": {"comment": {"id": 24315, "votes": 3}}}
            if tool == "vote":
                return {"status": "OK", "data": {"ok": True}}
            if tool == "read_comment":
                return {"status": "OK", "data": {"comment": {"id": 24315, "votes": 4}}}
            raise AssertionError(tool)
        result = self.forum.execute(
            self.forum.parse_args(["vote", "comment", "24315"]), invoker=invoker
        )
        self.assertEqual(result["status"], "WRITE_VERIFIED")
