from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path


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
        [
            str(interpreter),
            "-c",
            "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
        ],
        text=True,
    ).strip()
    if version != "3.12":
        raise RuntimeError(f"Python ABI mismatch: {version} != 3.12")

    pip = env_dir / "bin" / "pip"
    wheels = root / "wheels"
    packages = (
        ["semble"]
        if tool == "semble"
        else ["cactus-needle", f"scikit-learn=={cfg['ranker_version']}"]
    )
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


def prepare_local(
    *,
    tool: str,
    profiles: dict,
    input_status: str,
    mechanics: dict,
    runtime_dir: Path,
    env_dir: Path,
) -> dict:
    cfg = profiles[tool]
    receipt = {
        "schema_version": 1,
        "tool": tool,
        "status": "PENDING",
        "input_status": input_status,
        "freshness": mechanics.get("freshness"),
        "package": mechanics.get("package"),
        "blocking": False,
    }

    if input_status == "NO_SIGNAL":
        receipt["status"] = "SKIPPED_NO_SIGNAL"
        receipt["setup_ms"] = 0.0
        return receipt

    package = mechanics.get("package") or {}
    if package.get("status") != "READY":
        receipt["status"] = "UNAVAILABLE"
        receipt["setup_ms"] = 0.0
        receipt["error"] = "shared mechanics package is not READY"
        return receipt

    started = time.perf_counter()
    try:
        build_receipt = _verify_build_receipt(tool, cfg, runtime_dir)
        runtime = _install_python(tool, cfg, runtime_dir, env_dir)
        receipt.update(
            {
                "status": "READY",
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
