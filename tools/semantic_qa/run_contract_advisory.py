from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

try:
    from .prepare_rank_toolchain import prepare
except ImportError:
    from prepare_rank_toolchain import prepare


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool", choices=["semble", "needle3"], required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--input-manifest", type=Path, required=True)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--corpus-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--download-dir", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--env-dir", type=Path, required=True)
    args = parser.parse_args()

    started = time.perf_counter()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = args.output_dir / "receipt.json"
    toolchain_path = args.output_dir / "toolchain-receipt.json"
    profiles = json.loads(args.profile.read_text())
    manifest = json.loads(args.input_manifest.read_text())

    toolchain = prepare(
        tool=args.tool,
        profiles=profiles,
        input_status=manifest["status"],
        download_dir=args.download_dir,
        runtime_dir=args.runtime_dir,
        env_dir=args.env_dir,
    )
    _write(toolchain_path, toolchain)

    if toolchain["status"] == "UNAVAILABLE":
        payload = {
            "schema_version": 1,
            "tool": args.tool,
            "status": "UNAVAILABLE",
            "repository": args.repository,
            "task_id": args.task_id,
            "base_sha": args.base_sha,
            "candidate_sha": args.candidate_sha,
            "reason": "toolchain_unavailable",
            "acceptance_authority": False,
            "toolchain": toolchain,
            "orchestration_ms": (time.perf_counter() - started) * 1000,
        }
        _write(receipt_path, payload)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    runtime = toolchain.get("runtime") or {}
    interpreter = runtime.get("interpreter") or os.sys.executable
    env = os.environ.copy()
    if toolchain["status"] == "READY":
        env["PATH"] = str(Path(runtime["env_dir"]) / "bin") + os.pathsep + env.get("PATH", "")
        env["HF_HUB_OFFLINE"] = "1"

    if args.tool == "semble":
        if toolchain["status"] == "READY":
            env["SEMBLE_MODEL_NAME"] = runtime["model_dir"]
            env["SEMBLE_CACHE_LOCATION"] = str(args.output_dir / "semble-cache")
        script = "tools/semantic_qa/semble_contract_trace.py"
    else:
        env["NEEDLE_TELEMETRY"] = "0"
        script = "tools/semantic_qa/needle_contract_trace.py"

    command = [
        interpreter,
        script,
        "--input-manifest",
        str(args.input_manifest),
        "--input-root",
        str(args.input_root),
        "--corpus-dir",
        str(args.corpus_dir),
        "--profile",
        str(args.profile),
        "--output",
        str(receipt_path),
        "--top-k",
        str(profiles[args.tool]["top_k"]),
    ]
    if args.tool == "needle3":
        command += ["--snapshot", str(args.runtime_dir / "model")]

    cp = subprocess.run(command, env=env, text=True, capture_output=True)
    (args.output_dir / "runner.log").write_text(
        cp.stdout + "\n--- stderr ---\n" + cp.stderr
    )
    if cp.returncode != 0 or not receipt_path.is_file():
        payload = {
            "schema_version": 1,
            "tool": args.tool,
            "status": "UNAVAILABLE",
            "repository": args.repository,
            "task_id": args.task_id,
            "base_sha": args.base_sha,
            "candidate_sha": args.candidate_sha,
            "reason": "trace_execution_failed",
            "returncode": cp.returncode,
            "error_tail": cp.stderr[-4000:],
            "acceptance_authority": False,
            "toolchain": toolchain,
            "orchestration_ms": (time.perf_counter() - started) * 1000,
        }
    else:
        payload = json.loads(receipt_path.read_text())
        payload["toolchain"] = toolchain
        payload["orchestration_ms"] = (time.perf_counter() - started) * 1000
    _write(receipt_path, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
