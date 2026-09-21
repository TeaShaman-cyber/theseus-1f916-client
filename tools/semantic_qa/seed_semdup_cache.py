from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from run_semdup_advisory import _download_package, _freshness, _sha256, _write


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--download-dir", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    started = time.perf_counter()
    profile = json.loads(args.profile.read_text())
    cfg = profile["semdup"]
    package = cfg["package"]
    seed_sha = subprocess.check_output(
        ["git", "-C", str(args.repo), "rev-parse", "HEAD"], text=True
    ).strip()

    metadata, build_receipt = _download_package(package, args.download_dir, args.runtime_dir)
    if build_receipt.get("source_sha") != cfg["source_sha"]:
        raise RuntimeError("package source SHA mismatch")
    if build_receipt.get("model_key") != cfg["model_key"]:
        raise RuntimeError("package model key mismatch")

    binary = args.runtime_dir / "bin" / "semdup"
    binary.chmod(binary.stat().st_mode | 0o111)
    args.db_path.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["PATH"] = str(binary.parent) + os.pathsep + env.get("PATH", "")
    env["SEMDUP_CACHE"] = str(args.runtime_dir / "cache")

    run_started = time.perf_counter()
    cp = subprocess.run(
        [
            str(binary),
            "--db",
            str(args.db_path),
            "ci",
            "--mode",
            "scan",
            "--min-lines",
            str(cfg["min_lines"]),
            "--model",
            cfg["model_key"],
            "--provider",
            "cpu",
        ],
        cwd=args.repo,
        env=env,
        text=True,
        capture_output=True,
    )
    scan_ms = (time.perf_counter() - run_started) * 1000
    args.output.parent.mkdir(parents=True, exist_ok=True)
    (args.output.parent / "seed.log").write_text(
        cp.stdout + "\n--- stderr ---\n" + cp.stderr
    )
    if cp.returncode != 0:
        raise RuntimeError(f"semdup seed scan failed ({cp.returncode}): {cp.stderr[-2000:]}")

    seed = {
        "schema_version": 1,
        "status": "SEEDED",
        "seed_sha": seed_sha,
        "source_sha": cfg["source_sha"],
        "model_key": cfg["model_key"],
        "seeded_at": datetime.now(timezone.utc).isoformat(),
        "db_sha256": _sha256(args.db_path),
        "db_bytes": args.db_path.stat().st_size,
        "package_artifact_id": metadata.get("id"),
        "package_artifact_digest": metadata.get("digest"),
    }
    seed_path = args.db_path.parent / "seed.json"
    _write(seed_path, seed)

    receipt = {
        **seed,
        "freshness": _freshness(cfg),
        "package_setup_and_scan_ms": (time.perf_counter() - started) * 1000,
        "scan_ms": scan_ms,
        "acceptance_authority": False,
    }
    _write(args.output, receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
