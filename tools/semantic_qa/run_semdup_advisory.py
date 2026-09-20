from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

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
    args = parser.parse_args()

    started = time.perf_counter()
    profiles = json.loads(args.profile.read_text())
    cfg = profiles["semdup"]
    package = cfg["package"]
    manifest = json.loads(args.input_manifest.read_text())
    args.output_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = args.output_dir / "receipt.json"

    freshness = _freshness(cfg)
    toolchain = {
        "schema_version": 1,
        "status": "PENDING",
        "source_repository": package["repository"],
        "package": package,
        "freshness": freshness,
        "blocking": False,
    }

    if manifest["status"] == "NO_SIGNAL":
        toolchain["status"] = "SKIPPED_NO_SIGNAL"
        toolchain["setup_ms"] = 0.0
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
            "toolchain": toolchain,
            "orchestration_ms": (time.perf_counter() - started) * 1000,
        }
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
        toolchain.update(
            {
                "status": "READY",
                "setup_ms": (time.perf_counter() - setup_started) * 1000,
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
        )
    except Exception as exc:
        toolchain.update(
            {
                "status": "UNAVAILABLE",
                "setup_ms": (time.perf_counter() - setup_started) * 1000,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        receipt = {
            "schema_version": 1,
            "tool": "semdup",
            "status": "UNAVAILABLE",
            "repository": args.repository,
            "task_id": args.task_id,
            "base_sha": args.base_sha,
            "candidate_sha": args.candidate_sha,
            "input_status": manifest["status"],
            "acceptance_authority": False,
            "toolchain": toolchain,
            "orchestration_ms": (time.perf_counter() - started) * 1000,
        }
        _write(receipt_path, receipt)
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0

    env = os.environ.copy()
    env["PATH"] = str(Path(toolchain["runtime"]["binary"]).parent) + os.pathsep + env.get("PATH", "")
    env["SEMDUP_CACHE"] = toolchain["runtime"]["cache_dir"]
    command = [
        sys.executable,
        "tools/semantic_qa/semdup_trace.py",
        "--repo",
        str(args.repo),
        "--base-sha",
        args.base_sha,
        "--candidate-sha",
        args.candidate_sha,
        "--repository",
        args.repository,
        "--task-id",
        args.task_id,
        "--profile",
        str(args.profile),
        "--input-manifest",
        str(args.input_manifest),
        "--output-dir",
        str(args.output_dir),
    ]
    cp = subprocess.run(command, env=env, text=True, capture_output=True)
    (args.output_dir / "runner.log").write_text(cp.stdout + "\n--- stderr ---\n" + cp.stderr)
    if cp.returncode != 0 or not receipt_path.is_file():
        receipt = {
            "schema_version": 1,
            "tool": "semdup",
            "status": "UNAVAILABLE",
            "repository": args.repository,
            "task_id": args.task_id,
            "base_sha": args.base_sha,
            "candidate_sha": args.candidate_sha,
            "input_status": manifest["status"],
            "reason": "trace_execution_failed",
            "returncode": cp.returncode,
            "error_tail": cp.stderr[-4000:],
            "acceptance_authority": False,
        }
    else:
        receipt = json.loads(receipt_path.read_text())

    receipt["toolchain"] = toolchain
    receipt["orchestration_ms"] = (time.perf_counter() - started) * 1000
    _write(receipt_path, receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
