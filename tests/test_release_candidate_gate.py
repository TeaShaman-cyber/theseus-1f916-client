import argparse
import importlib.util
import json
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "dev" / "release_candidate.py"
SPEC = importlib.util.spec_from_file_location("release_candidate", MODULE_PATH)
release_candidate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_candidate)

SHA = "a" * 40


def hosted_metadata(workflow):
    return {
        "status": "completed",
        "conclusion": "success",
        "headSha": SHA,
        "event": "workflow_dispatch",
        "workflowName": workflow,
        "url": "https://example.invalid/run",
    }


def canonical_log():
    return (
        f"verification_source_sha={SHA}\n"
        f"verification_checkout_sha={SHA}\n"
        "DEV_CHECK_PASS\n"
    )


def property_log():
    return (
        f"verification_source_sha={SHA}\n"
        f"verification_checkout_sha={SHA}\n"
        "Ran 8 tests in 1.2s\n"
        "PROPERTY_TEST_PASS suite=state-machines max_examples=250 properties=8\n"
        "PROPERTY_TEST_RUNTIME seconds=2 exit=0\n"
    )


class ReleaseCandidateReceiptTests(unittest.TestCase):
    def test_hosted_receipts_bind_head_source_and_checkout(self):
        canonical = release_candidate.validate_hosted_receipt(
            "canonical", hosted_metadata("QA"), canonical_log(), SHA, 100
        )
        prop = release_candidate.validate_hosted_receipt(
            "property", hosted_metadata("Property test"), property_log(), SHA, 101
        )
        self.assertEqual(canonical["source_sha"], SHA)
        self.assertEqual(canonical["checkout_sha"], SHA)
        self.assertEqual(canonical["marker"], "DEV_CHECK_PASS")
        self.assertEqual(prop["properties"], 8)
        self.assertEqual(prop["max_examples"], 250)

    def test_hosted_receipt_rejects_source_or_run_head_mismatch(self):
        with self.assertRaises(release_candidate.GateError):
            release_candidate.validate_hosted_receipt(
                "canonical",
                {**hosted_metadata("QA"), "headSha": "b" * 40},
                canonical_log(),
                SHA,
                100,
            )
        with self.assertRaises(release_candidate.GateError):
            release_candidate.validate_hosted_receipt(
                "canonical",
                hosted_metadata("QA"),
                canonical_log().replace(SHA, "b" * 40, 1),
                SHA,
                100,
            )

    def test_property_receipt_requires_success_marker_and_runtime_exit_zero(self):
        with self.assertRaises(release_candidate.GateError):
            release_candidate.validate_hosted_receipt(
                "property", hosted_metadata("Property test"),
                property_log().replace("PROPERTY_TEST_PASS", "PROPERTY_TEST_MISSING"),
                SHA, 101,
            )
        with self.assertRaises(release_candidate.GateError):
            release_candidate.validate_hosted_receipt(
                "property", hosted_metadata("Property test"),
                property_log().replace("exit=0", "exit=1"),
                SHA, 101,
            )

    def test_review_receipt_requires_exact_sha_disposition_and_independent_evidence(self):
        valid = {
            "schema_version": 1,
            "candidate_sha": SHA,
            "disposition": "NO_SUBSTANTIVE_UNRESOLVED_FINDINGS",
            "evidence": [
                {"channel": "1f916-community", "ref": "c71153", "independent": True}
            ],
        }
        result = release_candidate.validate_review_receipt(valid, SHA)
        self.assertEqual(result["independent_evidence_count"], 1)
        for mutation in (
            {**valid, "candidate_sha": "b" * 40},
            {**valid, "disposition": "UNKNOWN"},
            {**valid, "evidence": []},
            {**valid, "evidence": [{"channel": "self", "ref": "x", "independent": False}]},
        ):
            with self.assertRaises(release_candidate.GateError):
                release_candidate.validate_review_receipt(mutation, SHA)

    def test_wrapper_and_gate_contain_no_promotion_actions(self):
        text = (ROOT / "tools" / "dev" / "release_candidate.py").read_text()
        wrapper = (ROOT / "tools" / "dev" / "release-candidate").read_text()
        combined = text + wrapper
        for forbidden in (
            "gh release create",
            "git tag",
            "git push",
            "VERSION.write_text",
        ):
            self.assertNotIn(forbidden, combined)
        self.assertIn('"promotion_authorized": False', text)

    def test_receipt_path_inside_repo_is_forbidden_by_main_contract(self):
        text = (ROOT / "tools" / "dev" / "release_candidate.py").read_text()
        self.assertIn("RC receipt path must be outside the repository worktree", text)


class FakeRunner:
    def __init__(self, remote_sha=SHA, dirty=False):
        self.remote_sha = remote_sha
        self.dirty = dirty
        self.calls = []

    def run(self, argv):
        argv = tuple(argv)
        self.calls.append(argv)
        if argv == ("git", "fetch", "origin", "main", "--prune"):
            return ""
        if argv == ("git", "branch", "--show-current"):
            return "main\n"
        if argv == ("git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"):
            return "origin/main\n"
        if argv == ("git", "rev-parse", "HEAD"):
            return SHA + "\n"
        if argv == ("git", "rev-parse", "origin/main"):
            return self.remote_sha + "\n"
        if argv == ("git", "status", "--porcelain=v1", "--untracked-files=all"):
            return "?? unexpected.txt\n" if self.dirty else ""
        if argv == ("python3", "-m", "unittest", "tests.test_transport_conformance", "-v"):
            return "Ran 12 tests in 0.10s\nOK\n"
        if argv == ("bash", "tools/dev/check"):
            return "DEV_CHECK_PASS\n"
        if argv[:3] == ("gh", "run", "view") and "--json" in argv:
            run_id = int(argv[3])
            return json.dumps(
                hosted_metadata("QA" if run_id == 100 else "Property test")
            )
        if argv[:3] == ("gh", "run", "view") and argv[-1] == "--log":
            return canonical_log() if int(argv[3]) == 100 else property_log()
        raise AssertionError(f"unexpected command: {argv!r}")


class ReleaseCandidateEvaluationTests(unittest.TestCase):
    def _args(self, review_path):
        return argparse.Namespace(
            expected_sha=SHA,
            expected_version=(ROOT / "VERSION").read_text().strip(),
            canonical_run=100,
            property_run=101,
            review_receipt=str(review_path),
            repo="TeaShaman-cyber/theseus-1f916-client",
            receipt=None,
        )

    def _review(self, path):
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "candidate_sha": SHA,
                    "disposition": "NO_SUBSTANTIVE_UNRESOLVED_FINDINGS",
                    "evidence": [
                        {
                            "channel": "1f916-community",
                            "ref": "c71153",
                            "independent": True,
                        }
                    ],
                }
            )
        )

    def test_full_synthetic_gate_returns_rc_ready_without_promotion_authority(self):
        with tempfile.TemporaryDirectory() as td:
            review = pathlib.Path(td) / "review.json"
            self._review(review)
            runner = FakeRunner()
            receipt = release_candidate.evaluate_candidate(
                self._args(review), runner=runner, root=ROOT
            )
            self.assertEqual(receipt["status"], "RC_READY")
            self.assertFalse(receipt["promotion_authorized"])
            self.assertEqual(receipt["candidate_sha"], SHA)
            self.assertEqual(receipt["local"]["conformance_tests"], 12)
            self.assertEqual(receipt["hosted"]["canonical"]["marker"], "DEV_CHECK_PASS")
            self.assertEqual(receipt["hosted"]["property"]["properties"], 8)
            self.assertGreaterEqual(receipt["review"]["independent_evidence_count"], 1)
            self.assertIn(("git", "fetch", "origin", "main", "--prune"), runner.calls)

    def test_gate_rejects_stale_remote_before_local_qa(self):
        with tempfile.TemporaryDirectory() as td:
            review = pathlib.Path(td) / "review.json"
            self._review(review)
            runner = FakeRunner(remote_sha="b" * 40)
            with self.assertRaises(release_candidate.GateError):
                release_candidate.evaluate_candidate(
                    self._args(review), runner=runner, root=ROOT
                )
            self.assertNotIn(("bash", "tools/dev/check"), runner.calls)

    def test_gate_rejects_dirty_tree_before_local_qa(self):
        with tempfile.TemporaryDirectory() as td:
            review = pathlib.Path(td) / "review.json"
            self._review(review)
            runner = FakeRunner(dirty=True)
            with self.assertRaises(release_candidate.GateError):
                release_candidate.evaluate_candidate(
                    self._args(review), runner=runner, root=ROOT
                )
            self.assertNotIn(("bash", "tools/dev/check"), runner.calls)


if __name__ == "__main__":
    unittest.main()
