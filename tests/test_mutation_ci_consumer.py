from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "mutation-test.yml"
LOCK = ROOT / "requirements" / "ci-mutation.txt"
ENDPOINT = ROOT / "tools" / "ci" / "mutation-test"
CONFIG = ROOT / "setup.cfg"
LEDGER_WORKFLOW = ROOT / ".github" / "workflows" / "mutation-ledger-test.yml"
LEDGER_ENDPOINT = ROOT / "tools" / "ci" / "mutation-ledger-test"
LEDGER_CONFIG = ROOT / "tools" / "ci" / "mutation-ledger.cfg"
PROFILE_SHA = "0fa76f7aa1ccad2fb175591c9497157d8c60e481"


class MutationCiConsumerContractTest(unittest.TestCase):
    def test_caller_pins_exact_profile_is_read_only_and_state_scoped(self):
        text = WORKFLOW.read_text()
        expected = (
            "uses: TeaShaman-cyber/marcopolo-cookbook/.github/workflows/"
            f"reusable-mutation-test.yml@{PROFILE_SHA}"
        )
        self.assertIn(expected, text)
        self.assertIn("permissions:\n  contents: read", text)
        self.assertIn("paths:", text)
        self.assertIn("- forum_state.py", text)
        self.assertIn("- tests/test_durable_state.py", text)
        self.assertNotIn("push:", text)
        self.assertNotIn("continue-on-error", text)
        self.assertNotIn("secrets:", text)

    def test_mutation_lock_is_fully_hash_pinned(self):
        text = LOCK.read_text()
        self.assertIn("mutmut==3.8.0", text)
        hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", text)
        packages = [line for line in text.splitlines() if line and not line.startswith("#") and "==" in line]
        self.assertEqual(len(packages), 19)
        self.assertEqual(len(hashes), 19)
        self.assertEqual(len(set(hashes)), 19)

    def test_mutmut_config_targets_state_and_deterministic_tests_only(self):
        text = CONFIG.read_text()
        self.assertIn("only_mutate=forum_state.py", text)
        self.assertIn("pytest_add_cli_args_test_selection=tests/test_durable_state.py", text)
        self.assertIn("process_isolation=fork", text)
        self.assertNotIn("property_tests", text)
        self.assertNotIn("hypothesis", text.lower())

    def test_endpoint_uses_exported_stats_not_raw_run_exit_as_verdict(self):
        result = subprocess.run(["sh", "-n", str(ENDPOINT)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        text = ENDPOINT.read_text()
        self.assertIn("mutmut export-cicd-stats", text)
        self.assertIn("mutmut-cicd-stats.json", text)
        for status in (
            "MUTATION_CLEAN",
            "SURVIVORS_PRESENT",
            "MUTATION_INCOMPLETE",
            "MUTATION_RUNTIME_FAILED",
        ):
            self.assertIn(status, text)
        self.assertIn("MUTATION_TEST_RECEIPT", text)
        self.assertIn("RUNNER_TEMP", text)

    def test_ledger_caller_pins_same_profile_and_is_read_only_and_bounded(self):
        text = LEDGER_WORKFLOW.read_text()
        expected = (
            "uses: TeaShaman-cyber/marcopolo-cookbook/.github/workflows/"
            f"reusable-mutation-test.yml@{PROFILE_SHA}"
        )
        self.assertIn(expected, text)
        self.assertIn("permissions:\n  contents: read", text)
        self.assertIn("- forum_ledger.py", text)
        self.assertIn("- tests/test_execution_ledger.py", text)
        self.assertIn("- tests/test_reconciliation.py", text)
        self.assertIn("mutation_endpoint: tools/ci/mutation-ledger-test", text)
        self.assertNotIn("push:", text)
        self.assertNotIn("continue-on-error", text)
        self.assertNotIn("secrets:", text)

    def test_ledger_mutmut_config_targets_ledger_and_reconciliation_tests_only(self):
        text = LEDGER_CONFIG.read_text()
        self.assertIn("only_mutate=forum_ledger.py", text)
        self.assertIn(
            'pytest_add_cli_args_test_selection=["tests/test_execution_ledger.py", "tests/test_reconciliation.py"]',
            text,
        )
        self.assertIn("process_isolation=fork", text)
        self.assertNotIn("forum_state.py", text)
        self.assertNotIn("property_tests", text)
        self.assertNotIn("hypothesis", text.lower())

    def test_ledger_endpoint_uses_ephemeral_profile_and_exported_stats(self):
        result = subprocess.run(
            ["sh", "-n", str(LEDGER_ENDPOINT)], capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        text = LEDGER_ENDPOINT.read_text()
        self.assertIn('cp "$ROOT/tools/ci/mutation-ledger.cfg" "$WORK_DIR/setup.cfg"', text)
        self.assertIn("mutmut export-cicd-stats", text)
        self.assertIn('"profile":"ledger_reconciliation"', text)
        self.assertIn('"only_mutate":"forum_ledger.py"', text)
        self.assertIn('"tests":"tests/test_execution_ledger.py tests/test_reconciliation.py"', text)
        self.assertIn('classified != total', text)
        self.assertIn('"unclassified_mutants":max(total-classified,0)', text)
        self.assertIn("MUTATION_TEST_RECEIPT", text)


if __name__ == "__main__":
    unittest.main()
