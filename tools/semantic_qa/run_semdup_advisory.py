from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

try:
    from .package_freshness import _git_head, evaluate_freshness
except ImportError:
    from package_freshness import _git_head, evaluate_freshness


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _freshness(cfg: dict) -> dict:
    package = cfg["package"]
    policy = cfg["freshness"]
    errors: list[str] = []
    current_git = None
    try:
        current_git = _git_head(policy["git_url"], policy["git_ref"])
    except Exception as exc:
        errors.append(f"git_currentness:{type(exc).__name__}:{exc}")
    result = evaluate_freshness(
        now=datetime.now(timezone.utc),
        built_at=package.get("built_at"),
        expires_at=package.get("expires_at"),
        max_age_days=int(policy.get("max_age_days", 30)),
        expiry_warning_days=int(policy.get("expiry_warning_days", 14)),
        pinned_git_sha=cfg["source_sha"],
        current_git_sha=current_git,
        currentness_errors=errors,
    )
    return {
        **result,
        "pinned_git_sha": cfg["source_sha"],
        "current_git_sha": current_git,
        "blocking": False,
    }


def _gh_json(endpoint: str) -> dict:
    cp = subprocess.run(
        ["gh", "api", "-H", "Accept: application/vnd.github+json", endpoint],
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    return json.loads(cp.stdout)


def _download_package(package: dict, download_dir: Path, runtime_dir: Path) -> tuple[dict, dict]:
    repository = package["repository"]
    artifact_id = int(package["artifact_id"])
    endpoint = f"repos/{repository}/actions/artifacts/{artifact_id}"
    metadata = _gh_json(endpoint)
    if metadata.get("expired"):
        raise RuntimeError("pinned package artifact is expired")
    if metadata.get("name") != package["artifact_name"]:
        raise RuntimeError("artifact name mismatch")
    if metadata.get("digest") != package["artifact_digest"]:
        raise RuntimeError("artifact digest mismatch")
    run = metadata.get("workflow_run") or {}
    if int(run.get("id", -1)) != int(package["run_id"]):
        raise RuntimeError("artifact workflow run mismatch")
    if run.get("head_sha") != package["builder_head_sha"]:
        raise RuntimeError("artifact builder head mismatch")

    shutil.rmtree(download_dir, ignore_errors=True)
    download_dir.mkdir(parents=True, exist_ok=True)
    archive = download_dir / "artifact.zip"
    with archive.open("wb") as stream:
        subprocess.run(
            ["gh", "api", "-H", "Accept: application/vnd.github+json", f"{endpoint}/zip"],
            stdout=stream,
            check=True,
            timeout=180,
        )
    expected_outer = package["artifact_digest"].removeprefix("sha256:")
    if _sha256(archive) != expected_outer:
        raise RuntimeError("downloaded artifact digest mismatch")

    unpacked = download_dir / "artifact"
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(unpacked)
    tar_path = unpacked / package["tar_file"]
    if _sha256(tar_path) != package["tar_sha256"]:
        raise RuntimeError("toolchain tar sha mismatch")

    shutil.rmtree(runtime_dir, ignore_errors=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r:gz") as tf:
        tf.extractall(runtime_dir, filter="data")

    build_receipt_path = runtime_dir / "build-receipt.json"
    if not build_receipt_path.is_file():
        raise RuntimeError("toolchain build receipt missing")
    build_receipt = json.loads(build_receipt_path.read_text())
    return metadata, build_receipt


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _degraded_receipt(
    *,
    args: argparse.Namespace,
    manifest: dict,
    freshness: dict,
    reason: str,
    cache: dict,
    started: float,
) -> dict:
    return {
        "schema_version": 1,
        "tool": "semdup",
        "status": "DEGRADED",
        "repository": args.repository,
        "task_id": args.task_id,
        "base_sha": args.base_sha,
        "candidate_sha": args.candidate_sha,
        "input_status": manifest["status"],
        "reason": reason,
        "acceptance_authority": False,
        "db_cache": cache,
        "toolchain": {
            "status": "SKIPPED",
            "freshness": freshness,
            "blocking": False,
        },
        "orchestration_ms": (time.perf_counter() - started) * 1000,
    }


def _finding_count(payload: object) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for key in ("findings", "pairs", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return len(value)
    return 0


def _run_diff(
    *,
    binary: Path,
    db_path: Path,
    repo: Path,
    base_sha: str,
    cfg: dict,
    out_json: Path,
    log_path: Path,
    env: dict[str, str],
) -> tuple[object, float]:
    started = time.perf_counter()
    cp = subprocess.run(
        [
            str(binary),
            "--db",
            str(db_path),
            "diff",
            "--base",
            base_sha,
            "--min-lines",
            str(cfg["min_lines"]),
            "--skip-tests",
            "--json",
            str(out_json),
            "--model",
            cfg["model_key"],
            "--provider",
            "cpu",
        ],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
    )
    wall_ms = (time.perf_counter() - started) * 1000
    log_path.write_text(cp.stdout + "\n--- stderr ---\n" + cp.stderr)
    if cp.returncode != 0:
        raise RuntimeError(f"semdup diff failed ({cp.returncode}): {cp.stderr[-2000:]}")
    payload = json.loads(out_json.read_text()) if out_json.exists() else []
    return payload, wall_ms


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--input-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--download-dir", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--db-cache-key", default="")
    args = parser.parse_args()

    started = time.perf_counter()
    profiles = json.loads(args.profile.read_text())
    cfg = profiles["semdup"]
    package = cfg["package"]
    manifest = json.loads(args.input_manifest.read_text())
    args.output_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = args.output_dir / "receipt.json"
    freshness = _freshness(cfg)

    if manifest["status"] == "NO_SIGNAL":
        receipt = {
            "schema_version": 1,
            "tool": "semdup",
            "status": "NO_SIGNAL",
            "repository": args.repository,
            "task_id": args.task_id,
            "base_sha": args.base_sha,
            "candidate_sha": args.candidate_sha,
            "input_status": "NO_SIGNAL",
            "acceptance_authority": False,
            "toolchain": {"status": "SKIPPED_NO_SIGNAL", "freshness": freshness, "blocking": False},
            "orchestration_ms": (time.perf_counter() - started) * 1000,
        }
        _write(receipt_path, receipt)
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0

    cache = {
        "status": "MISSING",
        "matched_key": args.db_cache_key,
        "expected_base_sha": args.base_sha,
        "path": str(args.db_path),
    }

    if manifest["status"] == "DEGRADED":
        receipt = _degraded_receipt(
            args=args,
            manifest=manifest,
            freshness=freshness,
            reason="input_budget_exceeded_semdup_full_diff_skipped",
            cache=cache,
            started=started,
        )
        _write(receipt_path, receipt)
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0

    seed_path = args.db_path.parent / "seed.json"
    if not args.db_path.is_file() or not seed_path.is_file():
        receipt = _degraded_receipt(
            args=args,
            manifest=manifest,
            freshness=freshness,
            reason="exact_base_db_cache_miss",
            cache=cache,
            started=started,
        )
        _write(receipt_path, receipt)
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0

    seed = json.loads(seed_path.read_text())
    cache.update({"status": "RESTORED", "seed": seed})
    if seed.get("seed_sha") != args.base_sha:
        receipt = _degraded_receipt(
            args=args,
            manifest=manifest,
            freshness=freshness,
            reason="db_cache_seed_sha_mismatch",
            cache=cache,
            started=started,
        )
        _write(receipt_path, receipt)
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0
    if seed.get("source_sha") != cfg["source_sha"] or seed.get("model_key") != cfg["model_key"]:
        receipt = _degraded_receipt(
            args=args,
            manifest=manifest,
            freshness=freshness,
            reason="db_cache_profile_mismatch",
            cache=cache,
            started=started,
        )
        _write(receipt_path, receipt)
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0

    setup_started = time.perf_counter()
    try:
        metadata, build_receipt = _download_package(package, args.download_dir, args.runtime_dir)
        if build_receipt.get("status") != "BUILT":
            raise RuntimeError("package build receipt is not BUILT")
        if build_receipt.get("source_sha") != cfg["source_sha"]:
            raise RuntimeError("package source SHA mismatch")
        if build_receipt.get("model_key") != cfg["model_key"]:
            raise RuntimeError("package model key mismatch")
        binary = args.runtime_dir / "bin" / "semdup"
        if not binary.is_file():
            raise RuntimeError("semdup binary missing from package")
        binary.chmod(binary.stat().st_mode | 0o111)
        toolchain = {
            "status": "READY",
            "setup_ms": (time.perf_counter() - setup_started) * 1000,
            "source_repository": package["repository"],
            "package": package,
            "freshness": freshness,
            "blocking": False,
            "artifact_metadata": {
                "id": metadata.get("id"),
                "name": metadata.get("name"),
                "digest": metadata.get("digest"),
                "expires_at": metadata.get("expires_at"),
            },
            "build_receipt": build_receipt,
            "runtime": {
                "binary": str(binary),
                "cache_dir": str(args.runtime_dir / "cache"),
            },
        }
    except Exception as exc:
        receipt = {
            "schema_version": 1,
            "tool": "semdup",
            "status": "UNAVAILABLE",
            "repository": args.repository,
            "task_id": args.task_id,
            "base_sha": args.base_sha,
            "candidate_sha": args.candidate_sha,
            "input_status": manifest["status"],
            "reason": "package_unavailable",
            "error": f"{type(exc).__name__}: {exc}",
            "acceptance_authority": False,
            "db_cache": cache,
            "toolchain": {
                "status": "UNAVAILABLE",
                "freshness": freshness,
                "blocking": False,
            },
            "orchestration_ms": (time.perf_counter() - started) * 1000,
        }
        _write(receipt_path, receipt)
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0

    env = os.environ.copy()
    env["PATH"] = str(binary.parent) + os.pathsep + env.get("PATH", "")
    env["SEMDUP_CACHE"] = toolchain["runtime"]["cache_dir"]

    try:
        cold_path = args.output_dir / "raw-cold.json"
        cold, cold_ms = _run_diff(
            binary=binary,
            db_path=args.db_path,
            repo=args.repo,
            base_sha=args.base_sha,
            cfg=cfg,
            out_json=cold_path,
            log_path=args.output_dir / "diff-cold.log",
            env=env,
        )
        warm_path = args.output_dir / "raw-warm.json"
        warm, warm_ms = _run_diff(
            binary=binary,
            db_path=args.db_path,
            repo=args.repo,
            base_sha=args.base_sha,
            cfg=cfg,
            out_json=warm_path,
            log_path=args.output_dir / "diff-warm.log",
            env=env,
        )
        status = "OK" if cold else "NO_SIGNAL"
        receipt = {
            "schema_version": 1,
            "tool": "semdup",
            "status": status,
            "repository": args.repository,
            "task_id": args.task_id,
            "base_sha": args.base_sha,
            "candidate_sha": args.candidate_sha,
            "input_status": manifest["status"],
            "acceptance_authority": False,
            "threshold": None,
            "evidence_only": True,
            "db_cache": cache,
            "toolchain": toolchain,
            "timing_ms": {"cold_diff": cold_ms, "warm_diff": warm_ms},
            "finding_count": {
                "cold": _finding_count(cold),
                "warm": _finding_count(warm),
            },
            "raw_json_sha256": {
                "cold": _sha256(cold_path),
                "warm": _sha256(warm_path),
            },
            "cold_findings": cold,
            "warm_findings": warm,
            "cold_warm_results_identical": cold == warm,
            "orchestration_ms": (time.perf_counter() - started) * 1000,
            "score_semantics": "evidence-only nearest-neighbor output; no threshold promoted",
        }
    except Exception as exc:
        receipt = {
            "schema_version": 1,
            "tool": "semdup",
            "status": "UNAVAILABLE",
            "repository": args.repository,
            "task_id": args.task_id,
            "base_sha": args.base_sha,
            "candidate_sha": args.candidate_sha,
            "input_status": manifest["status"],
            "reason": "incremental_diff_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "acceptance_authority": False,
            "db_cache": cache,
            "toolchain": toolchain,
            "orchestration_ms": (time.perf_counter() - started) * 1000,
        }

    _write(receipt_path, receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
