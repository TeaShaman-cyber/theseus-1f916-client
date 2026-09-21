from __future__ import annotations

import json
import unittest
from pathlib import Path

from tools.semantic_qa.contract_corpus import load_contract_corpus


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "docs/qa/semantic-contract-corpus"


class SemanticContractCorpusTests(unittest.TestCase):
    def test_records_are_exact_contract_excerpts(self):
        manifest, records = load_contract_corpus(CORPUS)
        self.assertEqual(manifest["source_path"], "docs/v1-contract.md")
        self.assertEqual(len(records), 8)
        self.assertEqual(
            [x["invariant_id"] for x in records],
            [
                "transport-identity-provenance",
                "write-route-no-auto-replay",
                "status-and-route-provenance",
                "durable-inbox-exact-cursor",
                "reconciliation-no-blind-replay",
                "cache-is-not-authority",
                "credential-custody-boundary",
                "promotion-authority-boundary",
            ],
        )

    def test_metadata_not_injected_into_record_text(self):
        _, records = load_contract_corpus(CORPUS)
        for record in records:
            self.assertNotIn(record["invariant_id"], record["text"])

    def test_profile_pins_cross_repo_semble_and_needle_packages(self):
        profile = json.loads((ROOT / "tools/semantic_qa/profile.json").read_text())
        self.assertEqual(
            profile["semble"]["package"]["repository"],
            "TeaShaman-cyber/theseus-research",
        )
        self.assertEqual(profile["semble"]["package"]["artifact_id"], 10613578514)
        self.assertEqual(
            profile["needle3"]["package"]["repository"],
            "TeaShaman-cyber/theseus-research",
        )
        self.assertEqual(profile["needle3"]["package"]["artifact_id"], 10612888067)


if __name__ == "__main__":
    unittest.main()
