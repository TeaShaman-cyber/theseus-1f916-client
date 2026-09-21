from __future__ import annotations

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
    from .package_freshness import _git_head, _hf_head, evaluate_freshness
except ImportError:
    from package_freshness import _git_head, _hf_head, evaluate_freshness


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
    current_hf = None
    try:
        current_git = _git_head(policy["git_url"], policy["git_ref"])
    except Exception as exc:
        errors.append(f"git_currentness:{type(exc).__name__}:{exc}")
    try:
        current_hf = _hf_head(policy["hf_repo"])
    except Exception as exc:
        errors.append(f"hf_currentness:{type(exc).__name__}:{exc}")
    result = evaluate_freshness(
        now=datetime.now(timezone.utc),
        built_at=package.get("built_at"),
        expires_at=package.get("expires_at"),
        max_age_days=int(policy.get("max_age_days", 30)),
        expiry_warning_days=int(policy.get("expiry_warning_days", 14)),
        pinned_git_sha=cfg.get("source_sha"),
        current_git_sha=current_git,
        pinned_hf_revision=cfg.get("model_revision"),
        current_hf_revision=current_hf,
        currentness_errors=errors,
    )
    return {
        **result,
        "pinned_git_sha": cfg.get("source_sha"),
        "current_git_sha": current_git,
        "pinned_hf_revision": cfg.get("model_revision"),
        "current_hf_revision": current_hf,
        "blocking": False,
    }


def _artifact_metadata(package: dict) -> dict:
    repository = package["repository"]
    cp = subprocess.run(
        [
            "gh",
            "api",
            "-H",
            "Accept: application/vnd.github+json",
            f"repos/{repository}/actions/artifacts/{package['artifact_id']}",
        ],
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    metadata = json.loads(cp.stdout)
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
    return metadata


def _download_and_extract(package: dict, download_dir: Path, runtime_dir: Path) -> Path:
    repository = package["repository"]
    artifact_id = int(package["artifact_id"])
    shutil.rmtree(download_dir, ignore_errors=True)
    download_dir.mkdir(parents=True, exist_ok=True)
    archive = download_dir / "artifact.zip"
    with archive.open("wb") as stream:
        subprocess.run(
            [
                "gh",
                "api",
                "-H",
                "Accept: application/vnd.github+json",
                f"repos/{repository}/actions/artifacts/{artifact_id}/zip",
            ],
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
    if not tar_path.is_file():
        raise RuntimeError("toolchain tarball missing from artifact")
    if _sha256(tar_path) != package["tar_sha256"]:
        raise RuntimeError("toolchain tar sha mismatch")

    shutil.rmtree(runtime_dir, ignore_errors=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r:gz") as tf:
        tf.extractall(runtime_dir, filter="data")
    return runtime_dir


def _verify_build_receipt(tool: str, cfg: dict, root: Path) -> dict:
    path = root / "build-receipt.json"
    if not path.is_file():
        raise RuntimeError("package build receipt missing")
    receipt = json.loads(path.read_text())
    if receipt.get("status") != "BUILT":
        raise RuntimeError("package build receipt is not BUILT")
    if receipt.get("source_sha") != cfg["source_sha"]:
        raise RuntimeError("package source SHA mismatch")
    if receipt.get("model_repo") != cfg["model_repo"]:
        raise RuntimeError("package model repo mismatch")
    if receipt.get("model_revision") != cfg["model_revision"]:
        raise RuntimeError("package model revision mismatch")
    return receipt


def _install_python(tool: str, cfg: dict, root: Path, env_dir: Path) -> dict:
    shutil.rmtree(env_dir, ignore_errors=True)
    subprocess.run([os.sys.executable, "-m", "venv", str(env_dir)], check=True)
    interpreter = env_dir / "bin" / "python"
    version = subprocess.check_output(
        [str(interpreter), "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        text=True,
    ).strip()
    if version != "3.12":
        raise RuntimeError(f"Python ABI mismatch: {version} != 3.12")
    pip = env_dir / "bin" / "pip"
    wheels = root / "wheels"
    packages = ["semble"] if tool == "semble" else [
        "cactus-needle",
        f"scikit-learn=={cfg['ranker_version']}",
    ]
    subprocess.run(
        [
            str(pip),
            "install",
            "--disable-pip-version-check",
            "--no-index",
            "--find-links",
            str(wheels),
            *packages,
        ],
        check=True,
        timeout=180,
    )
    return {
        "env_dir": str(env_dir),
        "interpreter": str(interpreter),
        "model_dir": str(root / "model"),
    }


def prepare(
    *,
    tool: str,
    profiles: dict,
    input_status: str,
    download_dir: Path,
    runtime_dir: Path,
    env_dir: Path,
) -> dict:
    cfg = profiles[tool]
    package = cfg["package"]
    receipt = {
        "schema_version": 1,
        "tool": tool,
        "status": "PENDING",
        "package": package,
        "freshness": _freshness(cfg),
        "input_status": input_status,
        "blocking": False,
    }
    if input_status == "NO_SIGNAL":
        receipt["status"] = "SKIPPED_NO_SIGNAL"
        receipt["setup_ms"] = 0.0
        return receipt

    started = time.perf_counter()
    try:
        metadata = _artifact_metadata(package)
        root = _download_and_extract(package, download_dir, runtime_dir)
        build_receipt = _verify_build_receipt(tool, cfg, root)
        runtime = _install_python(tool, cfg, root, env_dir)
        receipt.update(
            {
                "status": "READY",
                "artifact_metadata": {
                    "id": metadata.get("id"),
                    "name": metadata.get("name"),
                    "digest": metadata.get("digest"),
                    "expires_at": metadata.get("expires_at"),
                },
                "build_receipt": build_receipt,
                "runtime": runtime,
            }
        )
    except Exception as exc:
        receipt.update(
            {
                "status": "UNAVAILABLE",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
    receipt["setup_ms"] = (time.perf_counter() - started) * 1000
    return receipt
