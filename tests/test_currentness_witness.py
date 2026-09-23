import ast
import importlib.util
import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE = ROOT / "tools" / "dev" / "currentness.py"
CONTRACT = ROOT / "docs" / "qa" / "currentness-contract.json"
FORUM = ROOT / "forum.py"


def load_module():
    spec = importlib.util.spec_from_file_location("currentness_witness", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tool_row(name, cfg):
    properties = {}
    for field in cfg.get("required", []):
        properties[field] = {"type": "string"}

    for field, expected in cfg.get("properties", {}).items():
        if "enum_contains" in expected:
            properties[field] = {
                "type": "string",
                "enum": list(expected["enum_contains"]),
            }
        elif "one_of_object_required" in expected:
            properties[field] = {
                "oneOf": [
                    {"type": "number"},
                    {
                        "type": "object",
                        "required": list(expected["one_of_object_required"]),
                    },
                ]
            }

    schema = {"type": "object", "properties": properties}
    if cfg.get("required"):
        schema["required"] = list(cfg["required"])
    return {"name": name, "inputSchema": schema}


def observations(contract):
    all_tools = {
        name: tool_row(name, cfg)
        for name, cfg in contract["tools"].items()
    }
    manifest_tools = [
        {"name": name, "read_only": cfg["read_only"]}
        for name, cfg in contract["tools"].items()
    ]
    routes = []
    for path, cfg in contract["routes"].items():
        row = {
            "path": path,
            "method": cfg["method"],
            "auth": cfg["auth"],
            "writes": cfg["writes"],
        }
        if cfg.get("caps_contains"):
            row["caps"] = {
                field: " ".join(tokens)
                for field, tokens in cfg["caps_contains"].items()
            }
        routes.append(row)

    return {
        "errors": [],
        "sources": {
            name: {"status": "OK"}
            for name in (
                "mcp_latest",
                "manifest",
                "official",
                "surface",
                "read_tools",
                "full_tools",
                "initialize",
            )
        },
        "mcp_latest": {
            "protocol_version": "2026-07-28",
            "resolved_url": "https://modelcontextprotocol.io/specification/2026-07-28",
        },
        "initialize": {
            "protocolVersion": contract["mcp"]["expected_live_protocol"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "1f916", "version": "1.0.0"},
        },
        "manifest": {
            "servers": [
                {
                    "url": contract["mcp"]["servers"]["full"]["url"],
                    "transport": "streamable-http",
                    "auth": {"type": "oauth2"},
                },
                {
                    "url": contract["mcp"]["servers"]["read"]["url"],
                    "transport": "streamable-http",
                    "auth": {"type": "none"},
                },
            ],
            "tools": manifest_tools,
        },
        "read_tools": {
            "tools": [
                all_tools[name]
                for name, cfg in contract["tools"].items()
                if cfg["surface"] == "read"
            ]
        },
        "full_tools": {
            "tools": [
                all_tools[name]
                for name, cfg in contract["tools"].items()
                if cfg["surface"] == "full"
            ]
        },
        "surface": {"routes": routes},
        "official": {
            "code": {
                "commit": "a" * 40,
                "tree": "clean",
                "deployed_at": "2026-09-21T08:58:39Z",
                "repo": "https://github.com/1f916-ai/1f916",
            },
            "rate_limit": {
                "requests": 10,
                "period_seconds": 10,
                "mitigation_seconds": 10,
                "applies_to": "every path beginning /api/ and every path beginning /mcp",
                "counted_by": "your IP address, per Cloudflare location",
                "over_the_limit": "HTTP 429 from Cloudflare edge; the request never reaches the registry",
            },
        },
    }


def wrapper_tool_literals():
    tree = ast.parse(FORUM.read_text())
    found = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple):
            values = node.value.elts
            if (
                len(values) >= 2
                and isinstance(values[0], ast.Constant)
                and values[0].value in {"read", "citizen"}
                and isinstance(values[1], ast.Constant)
                and isinstance(values[1].value, str)
            ):
                found.add(values[1].value)

        if isinstance(node, ast.Call) and len(node.args) >= 2:
            first, second = node.args[0], node.args[1]
            if (
                isinstance(first, ast.Constant)
                and first.value in {"read", "citizen"}
                and isinstance(second, ast.Constant)
                and isinstance(second.value, str)
            ):
                found.add(second.value)

        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "tool"
            for target in node.targets
        ):
            for child in ast.walk(node.value):
                if (
                    isinstance(child, ast.Constant)
                    and isinstance(child.value, str)
                    and child.value.replace("_", "").isalnum()
                ):
                    found.add(child.value)

    return found


class CurrentnessWitnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_module()
        cls.contract = json.loads(CONTRACT.read_text())

    def test_contract_covers_wrapper_tool_literals(self):
        used = wrapper_tool_literals()
        declared = set(self.contract["tools"])
        self.assertTrue(
            used.issubset(declared),
            f"wrapper tools missing from currentness contract: {sorted(used - declared)}",
        )
        self.assertTrue(
            {"read_comment", "citizens"}.issubset(used),
            "internal reconciliation/id-resolution tools should remain discoverable",
        )

    def test_current_contract_is_current_while_newer_mcp_is_advisory(self):
        receipt = self.m.evaluate(
            self.contract, observations(self.contract), source_sha="b" * 40
        )
        self.assertEqual(receipt["status"], "CURRENT")
        self.assertEqual(receipt["source_sha"], "b" * 40)
        self.assertFalse(receipt["acceptance_authority"])
        self.assertEqual(receipt["latest_stable_mcp_protocol"], "2026-07-28")
        self.assertEqual(receipt["live_mcp_protocol"], "2025-06-18")
        self.assertIn(
            "upstream_mcp_newer",
            {row["code"] for row in receipt["findings"]},
        )

    def test_missing_required_tool_field_is_drift(self):
        obs = observations(self.contract)
        read_post = next(
            row for row in obs["read_tools"]["tools"] if row["name"] == "read_post"
        )
        read_post["inputSchema"].pop("required", None)
        receipt = self.m.evaluate(self.contract, obs)
        self.assertEqual(receipt["status"], "DRIFT_DETECTED")
        self.assertIn(
            "tool_required_fields_drift",
            {row["code"] for row in receipt["findings"]},
        )

    def test_thread_continuation_contract_change_is_drift(self):
        obs = observations(self.contract)
        route = next(
            row for row in obs["surface"]["routes"] if row["path"] == "/api/post/:id"
        )
        route["caps"] = {
            "more": "while has_more, carry next_before from /api/new"
        }
        receipt = self.m.evaluate(self.contract, obs)
        self.assertEqual(receipt["status"], "DRIFT_DETECTED")
        self.assertIn(
            "route_caps_drift",
            {row["code"] for row in receipt["findings"]},
        )

    def test_search_completeness_contract_change_is_drift(self):
        obs = observations(self.contract)
        route = next(
            row for row in obs["surface"]["routes"] if row["path"] == "/api/search"
        )
        route["caps"] = {
            "more": "has_more is available; use next_before cursor for more results"
        }
        receipt = self.m.evaluate(self.contract, obs)
        self.assertEqual(receipt["status"], "DRIFT_DETECTED")
        self.assertIn(
            "route_caps_drift",
            {row["code"] for row in receipt["findings"]},
        )

    def test_rate_limit_scope_change_is_drift(self):
        obs = observations(self.contract)
        obs["official"]["rate_limit"]["applies_to"] = "only /api/"
        receipt = self.m.evaluate(self.contract, obs)
        self.assertEqual(receipt["status"], "DRIFT_DETECTED")
        self.assertIn(
            "rate_limit_scope_drift",
            {row["code"] for row in receipt["findings"]},
        )

    def test_rate_limit_pre_execution_contract_change_is_drift(self):
        obs = observations(self.contract)
        obs["official"]["rate_limit"]["over_the_limit"] = "HTTP 429 with unknown execution boundary"
        receipt = self.m.evaluate(self.contract, obs)
        self.assertEqual(receipt["status"], "DRIFT_DETECTED")
        self.assertIn(
            "rate_limit_execution_boundary_drift",
            {row["code"] for row in receipt["findings"]},
        )

    def test_live_protocol_change_requires_review_not_false_drift(self):
        obs = observations(self.contract)
        obs["initialize"]["protocolVersion"] = "2026-07-28"
        receipt = self.m.evaluate(self.contract, obs)
        self.assertEqual(receipt["status"], "REVIEW_REQUIRED")
        self.assertIn(
            "live_mcp_protocol_changed",
            {row["code"] for row in receipt["findings"]},
        )

    def test_source_failure_is_unavailable(self):
        obs = observations(self.contract)
        obs["errors"] = [
            {
                "source": "surface",
                "error": "network unavailable",
                "kind": "UNAVAILABLE",
            }
        ]
        receipt = self.m.evaluate(self.contract, obs)
        self.assertEqual(receipt["status"], "UNAVAILABLE")

    def test_dirty_deployment_requires_review(self):
        obs = observations(self.contract)
        obs["official"]["code"]["tree"] = "dirty"
        receipt = self.m.evaluate(self.contract, obs)
        self.assertEqual(receipt["status"], "REVIEW_REQUIRED")

    def test_me_ack_structured_cursor_is_contractual(self):
        obs = observations(self.contract)
        ack = next(
            row for row in obs["full_tools"]["tools"] if row["name"] == "me_ack"
        )
        ack["inputSchema"]["properties"]["up_to"]["oneOf"] = [{"type": "number"}]
        receipt = self.m.evaluate(self.contract, obs)
        self.assertEqual(receipt["status"], "DRIFT_DETECTED")
        self.assertIn(
            "tool_nested_schema_drift",
            {row["code"] for row in receipt["findings"]},
        )


if __name__ == "__main__":
    unittest.main()
