from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "property-test.yml"
LOCK = ROOT / "requirements" / "ci-property.txt"
ENDPOINT = ROOT / "tools" / "ci" / "property-test"
STATE_PROPERTY = ROOT / "property_tests" / "test_state_properties.py"
LEDGER_PROPERTY = ROOT / "property_tests" / "test_ledger_properties.py"
STATEFUL_LEDGER = ROOT / "property_tests" / "test_ledger_state_machine.py"
PROFILE_SHA = "0fa76f7aa1ccad2fb175591c9497157d8c60e481"


class PropertyCiConsumerContractTest(unittest.TestCase):
    def test_caller_pins_exact_profile_and_is_read_only(self):
        text = WORKFLOW.read_text()
        expected = (
            "uses: TeaShaman-cyber/marcopolo-cookbook/.github/workflows/"
            f"reusable-property-test.yml@{PROFILE_SHA}"
        )
        self.assertIn(expected, text)
        self.assertEqual(len(PROFILE_SHA), 40)
        self.assertNotIn("@main", text)
        self.assertIn("permissions:\n  contents: read", text)
        self.assertNotIn("secrets:", text)
        self.assertNotIn("continue-on-error", text)

    def test_property_lock_is_exact_and_hash_pinned(self):
        text = LOCK.read_text()
        self.assertIn("hypothesis==6.168.0", text)
        self.assertIn("sortedcontainers==2.4.0", text)
        hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", text)
        self.assertEqual(len(hashes), 2)
        self.assertEqual(len(set(hashes)), 2)

    def test_endpoint_is_valid_posix_shell_and_scoped(self):
        result = subprocess.run(
            ["sh", "-n", str(ENDPOINT)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        text = ENDPOINT.read_text()
        self.assertIn("property_tests", text)
        self.assertIn("PROPERTY_TEST_PASS", text)
        self.assertIn("HYPOTHESIS_STORAGE_DIRECTORY", text)

    def test_property_settings_are_bounded_and_database_free(self):
        texts = [STATE_PROPERTY.read_text(), LEDGER_PROPERTY.read_text()]
        for text in texts:
            self.assertIn("max_examples=250", text)
            self.assertIn("deadline=None", text)
            self.assertIn("derandomize=True", text)
            self.assertIn("database=None", text)
            self.assertGreaterEqual(text.count("@PROPERTY_SETTINGS"), 3)

        stateful = STATEFUL_LEDGER.read_text()
        self.assertIn("RuleBasedStateMachine", stateful)
        self.assertIn("max_examples=60", stateful)
        self.assertIn("stateful_step_count=20", stateful)
        self.assertIn("deadline=None", stateful)
        self.assertIn("derandomize=True", stateful)
        self.assertIn("database=None", stateful)

        endpoint = ENDPOINT.read_text()
        self.assertIn("suite=property-and-stateful", endpoint)
        self.assertIn("deterministic=true", endpoint)
        self.assertNotIn("properties=8", endpoint)


if __name__ == "__main__":
    unittest.main()
