import importlib.util
import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CLIENT = ROOT / "client.py"


def load_client():
    spec = importlib.util.spec_from_file_location("forum_client_auth", CLIENT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ClientCredentialTests(unittest.TestCase):
    def setUp(self):
        self.client = load_client()

    def test_direct_environment_value_wins(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            runtime = root / "runtime.json"
            runtime.write_text(json.dumps({"secret": "runtime-value"}), encoding="utf-8")
            value = self.client.credential(
                env={"JESTER_FORUM_CREDENTIAL": "env-value"},
                root=root,
                runtime_path=runtime,
            )
            self.assertEqual(value, "env-value")

    def test_explicit_environment_file_wins_over_local_and_runtime(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            explicit = root / "explicit.json"
            local = root / "citizen.json"
            runtime = root / "runtime.json"
            explicit.write_text(json.dumps({"secret": "explicit-value"}), encoding="utf-8")
            local.write_text(json.dumps({"secret": "local-value"}), encoding="utf-8")
            runtime.write_text(json.dumps({"secret": "runtime-value"}), encoding="utf-8")
            value = self.client.credential(
                env={"JESTER_FORUM_CREDENTIAL_FILE": str(explicit)},
                root=root,
                runtime_path=runtime,
            )
            self.assertEqual(value, "explicit-value")

    def test_legacy_repo_local_file_remains_supported(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            local = root / "citizen.json"
            runtime = root / "runtime.json"
            local.write_text(json.dumps({"secret": "local-value"}), encoding="utf-8")
            runtime.write_text(json.dumps({"secret": "runtime-value"}), encoding="utf-8")
            value = self.client.credential(env={}, root=root, runtime_path=runtime)
            self.assertEqual(value, "local-value")

    def test_runtime_store_is_used_when_repo_local_file_is_absent(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            runtime = root / "runtime.json"
            runtime.write_text(json.dumps({"secret": "runtime-value"}), encoding="utf-8")
            value = self.client.credential(env={}, root=root, runtime_path=runtime)
            self.assertEqual(value, "runtime-value")

    def test_explicit_missing_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            runtime = root / "runtime.json"
            runtime.write_text(json.dumps({"secret": "runtime-value"}), encoding="utf-8")
            with self.assertRaises(FileNotFoundError):
                self.client.credential(
                    env={"JESTER_FORUM_CREDENTIAL_FILE": str(root / "missing.json")},
                    root=root,
                    runtime_path=runtime,
                )


if __name__ == "__main__":
    unittest.main()
