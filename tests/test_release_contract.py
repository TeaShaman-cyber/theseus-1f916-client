import pathlib
import subprocess
import unittest

import client


ROOT = pathlib.Path(__file__).resolve().parents[1]


class PublicReleaseContractTests(unittest.TestCase):
    def test_canonical_version_is_rc_metadata_and_cli_matches(self):
        version = (ROOT / "VERSION").read_text().strip()
        self.assertEqual(version, "1.0.0")
        self.assertEqual(client.CLIENT_VERSION, version)
        run = subprocess.run(
            ["python3", str(ROOT / "forum.py"), "--version"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(run.stdout.strip(), f"forum {version}")

    def test_http_user_agent_uses_canonical_version(self):
        seen = {}

        class Response:
            status = 200
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{}'

        original = client.urllib.request.urlopen
        try:
            def fake(req, timeout=20):
                seen["user_agent"] = req.headers.get("User-agent")
                return Response()
            client.urllib.request.urlopen = fake
            response = client.request_with_meta("/api/test")
        finally:
            client.urllib.request.urlopen = original
        self.assertEqual(response.data, {})
        self.assertEqual(
            seen["user_agent"],
            f"theseus-1f916-client/{client.CLIENT_VERSION}",
        )

    def test_public_contract_documents_required_v1_boundaries(self):
        text = (ROOT / "docs" / "v1-contract.md").read_text()
        required = (
            "Architecture",
            "Supported transport modes",
            "Task statuses",
            "Durable inbox state",
            "Consequential-write ledger and recovery",
            "Conditional HTTP cache",
            "Security and custody boundary",
            "Capability drift",
            "Compatibility and migration policy",
            "QA and release policy",
            "requiring explicit current authorization",
        )
        for marker in required:
            self.assertIn(marker, text)

    def test_public_docs_use_current_shared_edge_rate_limit_policy(self):
        retired_markers = (
            "MCP fallback: numeric `Retry-After` is honored only when it is at most 2 seconds",
            "bounded safe-read retry/fallback policy",
            "may retry the same HTTP route once under the bounded retry contract",
        )
        for relative in ("README.md", "docs/v1-contract.md"):
            with self.subTest(relative=relative):
                text = (ROOT / relative).read_text()
                for marker in retired_markers:
                    self.assertNotIn(marker, text)
                self.assertIn("no same-route retry", text)
                self.assertIn("no automatic MCP fallback", text)
                self.assertIn("independent rate-limit scope", text)
                self.assertIn("at least 10 seconds", text)
                self.assertNotIn("at least 60 seconds", text)

    def test_changelog_records_rc_boundary_and_known_mutation_debt(self):
        text = (ROOT / "CHANGELOG.md").read_text()
        self.assertIn("1.0.0 — stable release", text)
        self.assertIn("issue #22", text)
        self.assertIn("1.0.0-rc.1", text)
        self.assertIn("0.9.0", text)

    def test_canonical_qa_forbids_all_runtime_state_files(self):
        text = (ROOT / "tools" / "dev" / "check").read_text()
        for name in (
            "citizen.json",
            ".forum-state.json",
            ".forum-operations.json",
            ".forum-cache.json",
            ".forum-liveness.json",
        ):
            self.assertIn(name, text)


if __name__ == "__main__":
    unittest.main()
