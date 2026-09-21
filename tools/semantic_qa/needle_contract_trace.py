from __future__ import annotations

import argparse
import array
import hashlib
import json
import os
import shutil
import time
import zipfile
from pathlib import Path

try:
    from .contract_corpus import corpus_receipt, load_contract_corpus
except ImportError:
    from contract_corpus import corpus_receipt, load_contract_corpus


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _vector_hash(values: list[float]) -> str:
    return hashlib.sha256(array.array("f", values).tobytes()).hexdigest()


def _prepare_runtime(snapshot: Path, output_dir: Path, profile: dict) -> None:
    weights = snapshot / "needle3.cact"
    wheels = sorted(
        (snapshot / "python").glob(
            "cactus_needle-3.0.1-py3-none-manylinux2014_x86_64.whl"
        )
    )
    if len(wheels) != 1 or not weights.exists():
        raise RuntimeError("exact Needle weights/engine artifacts missing")
    wheel = wheels[0]
    if _sha256(weights) != profile["weights_sha256"]:
        raise RuntimeError("Needle weights SHA256 mismatch")
    if _sha256(wheel) != profile["engine_wheel_sha256"]:
        raise RuntimeError("Needle engine wheel SHA256 mismatch")

    engine_dir = output_dir / "engine"
    engine_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(wheel) as zf:
        candidates = [
            name
            for name in zf.namelist()
            if name.endswith("/libneedle3.so") or name == "needle/libneedle3.so"
        ]
        if len(candidates) != 1:
            raise RuntimeError("expected one libneedle3.so")
        engine = engine_dir / "libneedle3.so"
        engine.write_bytes(zf.read(candidates[0]))
    if _sha256(engine) != profile["engine_library_sha256"]:
        raise RuntimeError("Needle engine library SHA256 mismatch")

    cache_dir = Path.home() / ".cache" / "cactus-needle" / "v3" / profile["version"]
    cache_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(weights, cache_dir / "needle3.cact")

    os.environ["NEEDLE3_LIB_PATH"] = str(engine)
    os.environ["NEEDLE_TELEMETRY"] = "0"
    os.environ["HF_HUB_OFFLINE"] = "1"


def _run_pass(
    cases: list[dict],
    input_root: Path,
    records: list[dict],
    top_k: int,
) -> tuple[list[dict], float]:
    import needle
    import numpy as np
    from sklearn.neighbors import NearestNeighbors

    started = time.perf_counter()
    agent = needle.Needle(tools=[], auto_date=False)
    corpus_vectors = [agent.embed(record["text"]) for record in records]
    query_vectors = [
        agent.embed((input_root / case["patch_file"]).read_text(errors="replace"))
        for case in cases
    ]
    agent.close()

    matrix = np.asarray(corpus_vectors, dtype=np.float32)
    queries = np.asarray(query_vectors, dtype=np.float32)
    ranker = NearestNeighbors(
        n_neighbors=min(top_k, len(records)),
        algorithm="brute",
        metric="cosine",
        n_jobs=1,
    )
    ranker.fit(matrix)
    distances, indices = ranker.kneighbors(queries, return_distance=True)

    observed = []
    for case, query_vector, row_dist, row_idx in zip(
        cases, query_vectors, distances, indices
    ):
        observed.append(
            {
                "id": case["id"],
                "source_path": case["source_path"],
                "patch_sha256": case["sha256"],
                "query_vector_sha256_f32": _vector_hash(query_vector),
                "results": [
                    {
                        "rank": rank,
                        "invariant_id": records[int(idx)]["invariant_id"],
                        "record": records[int(idx)]["record"],
                        "cosine_distance": float(distance),
                    }
                    for rank, (distance, idx) in enumerate(
                        zip(row_dist, row_idx), start=1
                    )
                ],
            }
        )
    return observed, (time.perf_counter() - started) * 1000


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-manifest", type=Path, required=True)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--corpus-dir", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=3)
    args = parser.parse_args()

    manifest = json.loads(args.input_manifest.read_text())
    profile = json.loads(args.profile.read_text())["needle3"]
    args.output.parent.mkdir(parents=True, exist_ok=True)

    corpus_meta = corpus_receipt(args.corpus_dir)
    _, records = load_contract_corpus(args.corpus_dir)

    if manifest["status"] == "NO_SIGNAL":
        receipt = {
            "schema_version": 1,
            "tool": "needle3",
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

    _prepare_runtime(args.snapshot, args.output.parent, profile)

    import needle

    if needle.__version__ != profile["version"]:
        raise RuntimeError(
            f"Needle version mismatch: {needle.__version__} != {profile['version']}"
        )

    cold, cold_ms = _run_pass(
        manifest["selected"], args.input_root, records, args.top_k
    )
    warm, warm_ms = _run_pass(
        manifest["selected"], args.input_root, records, args.top_k
    )

    status = "DEGRADED" if manifest["status"] == "DEGRADED" else "OK"
    receipt = {
        "schema_version": 1,
        "tool": "needle3",
        "status": status,
        "repository": manifest["repository"],
        "task_id": manifest["task_id"],
        "base_sha": manifest["base_sha"],
        "candidate_sha": manifest["candidate_sha"],
        "input_status": manifest["status"],
        "selected_change_count": len(manifest["selected"]),
        "corpus": corpus_meta,
        "tool_profile": profile,
        "ranker": {
            "library": "scikit-learn",
            "class": "sklearn.neighbors.NearestNeighbors",
            "metric": profile["metric"],
            "algorithm": profile["algorithm"],
            "version": profile["ranker_version"],
            "custom_similarity_code": False,
        },
        "cold": {"wall_ms": cold_ms, "matches": cold},
        "warm": {"wall_ms": warm_ms, "matches": warm},
        "cold_warm_results_identical": cold == warm,
        "acceptance_authority": False,
        "score_semantics": "rank-only advisory evidence; not confidence or correctness",
    }
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
