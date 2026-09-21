from __future__ import annotations

import hashlib
import json
from pathlib import Path


def load_contract_corpus(corpus_dir: Path) -> tuple[dict, list[dict]]:
    manifest_path = corpus_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    source = Path(manifest["source_path"])
    source_bytes = source.read_bytes()
    got_source_sha = hashlib.sha256(source_bytes).hexdigest()
    if got_source_sha != manifest["source_sha256"]:
        raise RuntimeError(
            f"contract corpus source SHA mismatch: {got_source_sha} != {manifest['source_sha256']}"
        )

    source_lines = source.read_text().splitlines(keepends=True)
    observed = []
    for item in manifest["records"]:
        path = corpus_dir / item["record"]
        text = path.read_text()
        expected = "".join(
            source_lines[int(item["start_line"]) - 1 : int(item["end_line"])]
        )
        if text != expected:
            raise RuntimeError(f"contract corpus excerpt drift: {item['record']}")
        got = hashlib.sha256(text.encode()).hexdigest()
        if got != item["sha256"]:
            raise RuntimeError(f"contract corpus record SHA mismatch: {item['record']}")
        observed.append({**item, "text": text})
    return manifest, observed


def corpus_receipt(corpus_dir: Path) -> dict:
    manifest, records = load_contract_corpus(corpus_dir)
    return {
        "manifest_sha256": hashlib.sha256(
            (corpus_dir / "manifest.json").read_bytes()
        ).hexdigest(),
        "source_path": manifest["source_path"],
        "source_sha256": manifest["source_sha256"],
        "source_git_blob": manifest.get("source_git_blob"),
        "record_count": len(records),
        "records": [
            {
                "record": x["record"],
                "invariant_id": x["invariant_id"],
                "start_line": x["start_line"],
                "end_line": x["end_line"],
                "sha256": x["sha256"],
            }
            for x in records
        ],
    }
