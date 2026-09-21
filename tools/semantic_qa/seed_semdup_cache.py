from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from .run_semdup_advisory import _download_package, _freshness, _sha256, _write
except ImportError:
    from run_semdup_advisory import _download_package, _freshness, _sha256, _write


def _load_profile(path: Path) -> dict:
    return json.loads(path.read_text())["semdup"]


def prepare(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    cfg = _load_profile(args.profile)
    metadata, build_receipt = _download_package(
        cfg["package"], args.download_dir, args.runtime_dir
    )
    if build_receipt.get("source_sha") != cfg["source_sha"]:
        raise RuntimeError("package source SHA mismatch")
    if build_receipt.get("model_key") != cfg["model_key"]:
        raise RuntimeError("package model key mismatch")

    binary = args.runtime_dir / "bin" / "semdup"
    if not binary.is_file():
        raise RuntimeError("semdup binary missing from package")
    binary.chmod(binary.stat().st_mode | 0o111)

    payload = {
        "schema_version": 1,
        "status": "READY",
        "binary": str(binary),
        "cache_dir": str(args.runtime_dir / "cache"),
        "source_sha": cfg["source_sha"],
        "model_key": cfg["model_key"],
        "artifact_id": metadata.get("id"),
        "artifact_digest": metadata.get("digest"),
        "freshness": _freshness(cfg),
        "setup_ms": (time.perf_counter() - started) * 1000,
    }
    _write(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def finalize(args: argparse.Namespace) -> int:
    cfg = _load_profile(args.profile)
    seed_sha = subprocess.check_output(
        ["git", "-C", str(args.repo), "rev-parse", "HEAD"], text=True
    ).strip()
    if not args.db_path.is_file():
        raise RuntimeError("semdup DB missing after embed")

    runtime = json.loads(args.runtime.read_text())
    if runtime.get("status") != "READY":
        raise RuntimeError("runtime receipt is not READY")
    if runtime.get("source_sha") != cfg["source_sha"]:
        raise RuntimeError("runtime source SHA mismatch")
    if runtime.get("model_key") != cfg["model_key"]:
        raise RuntimeError("runtime model key mismatch")

    phases = {}
    for name in ("extract", "embed"):
        path = args.phase_dir / f"{name}.json"
        if not path.is_file():
            raise RuntimeError(f"{name} telemetry missing")
        phases[name] = json.loads(path.read_text())
        if phases[name].get("status") != "PASS":
            raise RuntimeError(f"{name} phase is not PASS")

    seed = {
        "schema_version": 1,
        "status": "SEEDED",
        "seed_sha": seed_sha,
        "source_sha": cfg["source_sha"],
        "model_key": cfg["model_key"],
        "seeded_at": datetime.now(timezone.utc).isoformat(),
        "db_sha256": _sha256(args.db_path),
        "db_bytes": args.db_path.stat().st_size,
        "package_artifact_id": runtime.get("artifact_id"),
        "package_artifact_digest": runtime.get("artifact_digest"),
    }
    _write(args.db_path.parent / "seed.json", seed)

    receipt = {
        **seed,
        "freshness": runtime.get("freshness"),
        "package_setup_ms": runtime.get("setup_ms"),
        "phases": phases,
        "total_observed_ms": (
            float(runtime.get("setup_ms") or 0)
            + float(phases["extract"].get("elapsed_ms") or 0)
            + float(phases["embed"].get("elapsed_ms") or 0)
        ),
        "acceptance_authority": False,
    }
    _write(args.output, receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    prep = sub.add_parser("prepare")
    prep.add_argument("--profile", type=Path, required=True)
    prep.add_argument("--download-dir", type=Path, required=True)
    prep.add_argument("--runtime-dir", type=Path, required=True)
    prep.add_argument("--output", type=Path, required=True)
    prep.set_defaults(func=prepare)

    fin = sub.add_parser("finalize")
    fin.add_argument("--repo", type=Path, default=Path("."))
    fin.add_argument("--profile", type=Path, required=True)
    fin.add_argument("--db-path", type=Path, required=True)
    fin.add_argument("--runtime", type=Path, required=True)
    fin.add_argument("--phase-dir", type=Path, required=True)
    fin.add_argument("--output", type=Path, required=True)
    fin.set_defaults(func=finalize)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
