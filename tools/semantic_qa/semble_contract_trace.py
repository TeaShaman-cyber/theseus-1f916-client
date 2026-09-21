from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

try:
    from .contract_corpus import corpus_receipt, load_contract_corpus
except ImportError:
    from contract_corpus import corpus_receipt, load_contract_corpus


def _dir_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _normalize(payload: dict, mapping: dict[str, str], top_k: int) -> list[dict]:
    out = []
    for rank, item in enumerate(payload.get("results", [])[:top_k], start=1):
        name = Path(item["file_path"]).name
        out.append(
            {
                "rank": rank,
                "invariant_id": mapping.get(name),
                "record": name,
                "score": item["score"],
                "start_line": int(item["start_line"]),
                "end_line": int(item["end_line"]),
            }
        )
    return out


def _run_pass(
    cases: list[dict],
    input_root: Path,
    corpus: Path,
    mapping: dict[str, str],
    top_k: int,
) -> tuple[list[dict], list[dict], float]:
    started = time.perf_counter()
    observed = []
    errors = []
    for case in cases:
        query = (input_root / case["patch_file"]).read_text(errors="replace")
        cp = subprocess.run(
            [
                "semble",
                "search",
                query,
                str(corpus),
                "--content",
                "docs",
                "--top-k",
                str(top_k),
                "--format",
                "json",
            ],
            text=True,
            capture_output=True,
        )
        if cp.returncode != 0:
            errors.append(
                {
                    "id": case["id"],
                    "returncode": cp.returncode,
                    "stderr": cp.stderr[-2000:],
                }
            )
            continue
        payload = json.loads(cp.stdout)
        observed.append(
            {
                "id": case["id"],
                "source_path": case["source_path"],
                "patch_sha256": case["sha256"],
                "results": _normalize(payload, mapping, top_k),
            }
        )
    return observed, errors, (time.perf_counter() - started) * 1000


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-manifest", type=Path, required=True)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--corpus-dir", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=3)
    args = parser.parse_args()

    manifest = json.loads(args.input_manifest.read_text())
    profile = json.loads(args.profile.read_text())["semble"]
    args.output.parent.mkdir(parents=True, exist_ok=True)

    corpus_meta = corpus_receipt(args.corpus_dir)
    _, records = load_contract_corpus(args.corpus_dir)

    if manifest["status"] == "NO_SIGNAL":
        receipt = {
            "schema_version": 1,
            "tool": "semble",
            "status": "NO_SIGNAL",
            "repository": manifest["repository"],
            "task_id": manifest["task_id"],
            "base_sha": manifest["base_sha"],
            "candidate_sha": manifest["candidate_sha"],
            "corpus": corpus_meta,
            "matches": [],
            "acceptance_authority": False,
        }
        args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        return 0

    with tempfile.TemporaryDirectory(prefix="client-contract-corpus-") as temp:
        projected = Path(temp)
        mapping = {}
        for record in records:
            name = record["record"]
            (projected / name).write_text(record["text"])
            mapping[name] = record["invariant_id"]

        cache_path = Path(os.environ["SEMBLE_CACHE_LOCATION"])
        before = _dir_bytes(cache_path)
        cold, cold_errors, cold_ms = _run_pass(
            manifest["selected"], args.input_root, projected, mapping, args.top_k
        )
        after_cold = _dir_bytes(cache_path)
        warm, warm_errors, warm_ms = _run_pass(
            manifest["selected"], args.input_root, projected, mapping, args.top_k
        )
        after_warm = _dir_bytes(cache_path)

    failures = len(cold_errors) + len(warm_errors)
    if cold_errors and len(cold_errors) == len(manifest["selected"]):
        status = "UNAVAILABLE"
    elif failures or manifest["status"] == "DEGRADED":
        status = "DEGRADED"
    else:
        status = "OK"

    normalized_cold = [(x["id"], x["results"]) for x in cold]
    normalized_warm = [(x["id"], x["results"]) for x in warm]
    receipt = {
        "schema_version": 1,
        "tool": "semble",
        "status": status,
        "repository": manifest["repository"],
        "task_id": manifest["task_id"],
        "base_sha": manifest["base_sha"],
        "candidate_sha": manifest["candidate_sha"],
        "input_status": manifest["status"],
        "selected_change_count": len(manifest["selected"]),
        "corpus": corpus_meta,
        "tool_profile": profile,
        "cold": {"wall_ms": cold_ms, "matches": cold, "errors": cold_errors},
        "warm": {"wall_ms": warm_ms, "matches": warm, "errors": warm_errors},
        "cold_warm_results_identical": normalized_cold == normalized_warm,
        "cache": {
            "bytes_before": before,
            "bytes_after_cold": after_cold,
            "bytes_after_warm": after_warm,
            "warm_delta": after_warm - after_cold,
        },
        "acceptance_authority": False,
        "score_semantics": "rank-only advisory evidence; not confidence or correctness",
    }
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
