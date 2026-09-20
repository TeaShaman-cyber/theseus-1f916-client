from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "qa.yml"
COOKBOOK_SHA = "5c6a60df781adc0c6426c8cd25dcde857ee2435d"


class ReusableQaConsumerContractTest(unittest.TestCase):
    def test_consumer_preserves_existing_triggers_and_read_only_permissions(self):
        text = WORKFLOW.read_text()
        self.assertIn("push:", text)
        self.assertIn("pull_request:", text)
        self.assertIn("permissions:\n  contents: read", text)
        self.assertNotRegex(text, r"contents:\s+write")

    def test_consumer_pins_exact_cookbook_revision(self):
        text = WORKFLOW.read_text()
        expected = (
            "TeaShaman-cyber/marcopolo-cookbook/.github/workflows/"
            f"reusable-canonical-qa.yml@{COOKBOOK_SHA}"
        )
        self.assertIn(f"uses: {expected}", text)
        self.assertNotIn("@main", text)
        self.assertNotRegex(text, r"uses: .*@v\d+")

    def test_consumer_delegates_runner_mechanics_and_keeps_repo_owned_endpoint(self):
        text = WORKFLOW.read_text()
        job = text.split("  dev-check:\n", 1)[1]
        self.assertNotIn("runs-on:", job)
        self.assertNotIn("steps:", job)
        self.assertNotIn("actions/checkout", job)
        self.assertNotIn("actions/setup-python", job)
        self.assertIn('python_version: "3.11"', job)
        self.assertIn("qa_endpoint: tools/dev/check", job)

    def test_reusable_workflow_reference_uses_full_sha(self):
        text = WORKFLOW.read_text()
        match = re.search(r"reusable-canonical-qa\.yml@([0-9a-f]+)", text)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), COOKBOOK_SHA)
        self.assertEqual(len(match.group(1)), 40)


if __name__ == "__main__":
    unittest.main()
