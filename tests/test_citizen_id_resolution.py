from __future__ import annotations

import json
import subprocess
import unittest

import forum
import http_transport


class CitizenIdResolutionTests(unittest.TestCase):
    def test_explicit_numeric_id_walks_complete_census_then_reads_exact_handle(self):
        calls = []
        pages = {
            None: {
                "citizens": [
                    {"citizen_id": 1, "handle": "1f916-agent", "created_at": 100},
                    {"citizen_id": 2, "handle": "two", "created_at": 200},
                ],
                "has_more": True,
                "next_since": 200,
                "returned": 2,
                "total": 3,
            },
            200: {
                "citizens": [
                    {"citizen_id": 3, "handle": "three", "created_at": 300},
                ],
                "has_more": False,
                "next_since": None,
                "returned": 1,
                "total": 3,
            },
        }

        def invoker(surface, tool, payload):
            calls.append((surface, tool, dict(payload)))
            if tool == "citizens":
                return {"status": "OK", "data": pages[payload.get("since")]}
            if tool == "citizen":
                self.assertEqual(payload, {"handle": "1f916-agent"})
                return {
                    "status": "OK",
                    "data": {"handle": "1f916-agent", "posts": [], "comments": []},
                    "route": "http:GET /api/citizen/1f916-agent",
                }
            raise AssertionError(tool)

        result = forum.execute(
            forum.parse_args(["citizen", "--id", "1"]),
            invoker=invoker,
        )
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["data"]["handle"], "1f916-agent")
        resolution = result["identity_resolution"]
        self.assertEqual(resolution["requested_citizen_id"], 1)
        self.assertEqual(resolution["resolved_handle"], "1f916-agent")
        self.assertEqual(resolution["source"], "live_citizens")
        self.assertEqual(resolution["requested_transport"], "auto")
        self.assertEqual(resolution["pages_checked"], 2)
        self.assertEqual(resolution["rows_checked"], 3)
        self.assertTrue(resolution["coverage_complete"])
        self.assertEqual(len(resolution["census_page_provenance"]), 2)
        self.assertEqual(
            calls,
            [
                ("read", "citizens", {}),
                ("read", "citizens", {"since": 200}),
                ("read", "citizen", {"handle": "1f916-agent"}),
            ],
        )

    def test_cli_requires_exactly_one_handle_or_numeric_id(self):
        with self.assertRaises(SystemExit):
            forum.parse_args(["citizen"])
        with self.assertRaises(SystemExit):
            forum.parse_args(["citizen", "one", "--id", "1"])

    def test_numeric_looking_positional_value_remains_literal_handle(self):
        args = forum.parse_args(["citizen", "1"])
        self.assertEqual(args.handle, "1")
        self.assertIsNone(args.citizen_id)
        self.assertEqual(
            forum.build_call(args),
            ("read", "citizen", {"handle": "1"}),
        )

    def test_ordinary_handle_lookup_does_not_touch_census(self):
        calls = []

        def invoker(surface, tool, payload):
            calls.append((surface, tool, dict(payload)))
            return {"status": "OK", "data": {"handle": payload["handle"]}}

        result = forum.execute(
            forum.parse_args(["citizen", "lad-codex"]),
            invoker=invoker,
        )
        self.assertEqual(result["status"], "OK")
        self.assertNotIn("identity_resolution", result)
        self.assertEqual(
            calls,
            [("read", "citizen", {"handle": "lad-codex"})],
        )

    def test_stalled_census_fails_closed_without_exact_read(self):
        calls = []

        def invoker(surface, tool, payload):
            calls.append((surface, tool, dict(payload)))
            return {
                "status": "OK",
                "data": {
                    "citizens": [],
                    "has_more": True,
                    "next_since": payload.get("since"),
                    "returned": 0,
                    "total": 10,
                },
            }

        result = forum.execute(
            forum.parse_args(["citizen", "--id", "99"]),
            invoker=invoker,
        )
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("census", result["error"])
        self.assertFalse(any(tool == "citizen" for _, tool, _ in calls))

    def test_missing_id_after_complete_census_fails_closed(self):
        result = forum.execute(
            forum.parse_args(["citizen", "--id", "99"]),
            invoker=lambda surface, tool, payload: {
                "status": "OK",
                "data": {
                    "citizens": [{"citizen_id": 1, "handle": "one"}],
                    "has_more": False,
                    "next_since": None,
                    "returned": 1,
                    "total": 1,
                },
            },
        )
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("citizen_id 99", result["error"])

    def test_duplicate_id_in_complete_census_fails_closed(self):
        result = forum.execute(
            forum.parse_args(["citizen", "--id", "1"]),
            invoker=lambda surface, tool, payload: {
                "status": "OK",
                "data": {
                    "citizens": [
                        {"citizen_id": 1, "handle": "one"},
                        {"citizen_id": 1, "handle": "other"},
                    ],
                    "has_more": False,
                    "next_since": None,
                    "returned": 2,
                    "total": 2,
                },
            },
        )
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("exactly one", result["error"])

    def test_mcp_citizens_route_uses_reader_surface(self):
        seen = {}

        def runner(argv, **kwargs):
            seen["argv"] = argv
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout='{"citizens":[],"returned":0,"total":0,"has_more":false,"next_since":null}',
                stderr="",
            )

        result = forum.invoke(
            "read",
            "citizens",
            {"since": 123},
            runner=runner,
            base_env={},
        )
        self.assertEqual(result["status"], "OK")
        self.assertIn("forum-read.citizens", seen["argv"])
        args_index = seen["argv"].index("--args") + 1
        self.assertEqual(json.loads(seen["argv"][args_index]), {"since": 123})

    def test_http_citizens_route_uses_created_at_since(self):
        self.assertEqual(
            http_transport.call_spec("read", "citizens", {}),
            ("GET", "/api/citizens", None, False),
        )
        self.assertEqual(
            http_transport.call_spec("read", "citizens", {"since": 1787389057153}),
            ("GET", "/api/citizens?since=1787389057153", None, False),
        )


if __name__ == "__main__":
    unittest.main()
