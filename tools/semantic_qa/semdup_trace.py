from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path



def _run(cmd: list[str], *, cwd: Path, log: Path) -> float:
    started = time.perf_counter()
    cp = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    log.write_text(cp.stdout + "\n--- stderr ---\n" + cp.stderr)
    if cp.returncode != 0:
        raise RuntimeError(f"command failed ({cp.returncode}): {' '.join(cmd)}")
    return (time.perf_counter() - started) * 1000


def _dir_bytes(path: Path) -> int:
    return (
        sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
        if path.exists()
        else 0
    )



def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--input-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    profiles = json.loads(args.profile.read_text())
    profile = profiles["semdup"]
    manifest = json.loads(args.input_manifest.read_text())
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    receipt_path = out / "receipt.json"

    base_receipt = {
        "schema_version": 1,
        "tool": "semdup",
        "repository": args.repository,
        "task_id": args.task_id,
        "base_sha": args.base_sha,
        "candidate_sha": args.candidate_sha,
        "profile_sha256": hashlib.sha256(args.profile.read_bytes()).hexdigest(),
        "tool_profile": profile,
        "input_status": manifest["status"],
        "input_manifest_sha256": hashlib.sha256(args.input_manifest.read_bytes()).hexdigest(),
        "acceptance_authority": False,
    }

    if manifest["status"] == "NO_SIGNAL":
        receipt = {**base_receipt, "status": "NO_SIGNAL"}
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        return 0

    if manifest["status"] == "DEGRADED":
        receipt = {
            **base_receipt,
            "status": "DEGRADED",
            "reason": "input_budget_exceeded; semdup full diff skipped to preserve runner budget",
        }
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        return 0

    repo = args.repo.resolve()
    actual_head = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    if actual_head != args.candidate_sha:
        raise RuntimeError(f"checkout SHA mismatch: {actual_head} != {args.candidate_sha}")

    model = profile["model_key"]
    cache = Path(os.environ["SEMDUP_CACHE"])
    cache_before = _dir_bytes(cache)

    with tempfile.TemporaryDirectory(prefix="semdup-field-") as temp:
        temp_path = Path(temp)
        base_worktree = temp_path / "base"
        db = temp_path / "semdup.sqlite"
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "worktree",
                "add",
                "--detach",
                str(base_worktree),
                args.base_sha,
            ],
            check=True,
        )
        try:
            extract_ms = _run(
                [
                    "semdup",
                    "--db",
                    str(db),
                    "extract",
                    "--root",
                    str(base_worktree),
                    "--corpus",
                    "main",
                    "--respect-gitignore",
                    "--granularity",
                    profile["unit_kind"],
                ],
                cwd=repo,
                log=out / "extract.log",
            )
            embed_ms = _run(
                [
                    "semdup",
                    "--db",
                    str(db),
                    "embed",
                    "--model",
                    model,
                    "--provider",
                    "cpu",
                ],
                cwd=repo,
                log=out / "embed.log",
            )
            cache_after_cold = _dir_bytes(cache)

            cold_json = out / "raw-cold.json"
            cold_ms = _run(
                [
                    "semdup",
                    "--db",
                    str(db),
                    "diff",
                    "--base",
                    args.base_sha,
                    "--min-lines",
                    str(profile["min_lines"]),
                    "--skip-tests",
                    "--json",
                    str(cold_json),
                    "--model",
                    model,
                    "--provider",
                    "cpu",
                ],
                cwd=repo,
                log=out / "diff-cold.log",
            )
            warm_json = out / "raw-warm.json"
            warm_ms = _run(
                [
                    "semdup",
                    "--db",
                    str(db),
                    "diff",
                    "--base",
                    args.base_sha,
                    "--min-lines",
                    str(profile["min_lines"]),
                    "--skip-tests",
                    "--json",
                    str(warm_json),
                    "--model",
                    model,
                    "--provider",
                    "cpu",
                ],
                cwd=repo,
                log=out / "diff-warm.log",
            )
        finally:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "worktree",
                    "remove",
                    "--force",
                    str(base_worktree),
                ],
                check=False,
            )

    cold = json.loads(cold_json.read_text()) if cold_json.exists() else []
    warm = json.loads(warm_json.read_text()) if warm_json.exists() else []
    cache_after_warm = _dir_bytes(cache)
    receipt = {
        **base_receipt,
        "status": "OK" if cold else "NO_SIGNAL",
        "evidence_only": True,
        "threshold": None,
        "timing_ms": {
            "base_extract": extract_ms,
            "base_embed": embed_ms,
            "cold_diff": cold_ms,
            "warm_diff": warm_ms,
        },
        "cache": {
            "bytes_before": cache_before,
            "bytes_after_cold": cache_after_cold,
            "bytes_after_warm": cache_after_warm,
            "warm_delta": cache_after_warm - cache_after_cold,
        },
        "cold_findings": cold,
        "warm_findings": warm,
        "cold_warm_results_identical": cold == warm,
        "score_semantics": "upstream evidence-only nearest-neighbor output; no threshold promoted",
    }
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
