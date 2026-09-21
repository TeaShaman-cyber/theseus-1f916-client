from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.semantic_qa import seed_semdup_cache


class SeedTelemetryTests(unittest.TestCase):
    def test_finalize_records_split_phase_telemetry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile = root / "profile.json"
            db = root / "cache" / "semdup.sqlite"
            runtime = root / "runtime.json"
            phases = root / "phases"
            output = root / "receipt.json"
            phases.mkdir()
            db.parent.mkdir()
            db.write_bytes(b"sqlite-placeholder")
            profile.write_text(json.dumps({
                "semdup": {
                    "source_sha": "a" * 40,
                    "model_key": "model-key",
                }
            }))
            runtime.write_text(json.dumps({
                "status": "READY",
                "source_sha": "a" * 40,
                "model_key": "model-key",
                "artifact_id": 123,
                "artifact_digest": "sha256:abc",
                "freshness": {"status": "CURRENT"},
                "setup_ms": 12.5,
            }))
            (phases / "extract.json").write_text(json.dumps({
                "status": "PASS", "elapsed_ms": 100, "db_bytes": 10
            }))
            (phases / "embed.json").write_text(json.dumps({
                "status": "PASS", "elapsed_ms": 200, "db_bytes": 20
            }))

            args = mock.Mock(
                repo=root,
                profile=profile,
                db_path=db,
                runtime=runtime,
                phase_dir=phases,
                output=output,
            )
            with mock.patch("subprocess.check_output", return_value="b" * 40 + "\n"):
                rc = seed_semdup_cache.finalize(args)

            self.assertEqual(rc, 0)
            receipt = json.loads(output.read_text())
            self.assertEqual(receipt["phases"]["extract"]["elapsed_ms"], 100)
            self.assertEqual(receipt["phases"]["embed"]["elapsed_ms"], 200)
            self.assertEqual(receipt["total_observed_ms"], 312.5)
            self.assertEqual(receipt["status"], "SEEDED")


if __name__ == "__main__":
    unittest.main()
