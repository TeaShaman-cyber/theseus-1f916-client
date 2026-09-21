from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path


def _git(repo: Path, *args: str, text: bool = False):
    cmd = ["git", "-C", str(repo), *args]
    return subprocess.check_output(cmd, text=text)


def resolve_commit(repo: Path, value: str) -> str:
    return _git(repo, "rev-parse", "--verify", f"{value}^{{commit}}", text=True).strip()


def _is_binary_change(repo: Path, base_sha: str, head_sha: str, path: str) -> bool:
    numstat = _git(
        repo,
        "diff",
        "--numstat",
        "--no-renames",
        base_sha,
        head_sha,
        "--",
        path,
        text=True,
    )
    for line in numstat.splitlines():
        fields = line.split("\t", 2)
        if len(fields) >= 2 and fields[0] == "-" and fields[1] == "-":
            return True
    return False


def build_manifest(
    repo: Path,
    base: str,
    head: str,
    output: Path,
    repository: str,
    task_id: str,
    *,
    max_files: int,
    max_total_bytes: int,
    max_file_bytes: int,
) -> dict:
    repo = repo.resolve()
    output.mkdir(parents=True, exist_ok=True)
    cases_dir = output / "cases"
    cases_dir.mkdir(parents=True, exist_ok=True)

    base_sha = resolve_commit(repo, base)
    head_sha = resolve_commit(repo, head)

    raw_paths = _git(
        repo,
        "diff",
        "--name-only",
        "-z",
        "--diff-filter=ACMRT",
        base_sha,
        head_sha,
        "--",
    )
    paths = sorted({os.fsdecode(p) for p in raw_paths.split(b"\0") if p})

    selected: list[dict] = []
    skipped: list[dict] = []
    total_bytes = 0
    degraded = False

    for path in paths:
        if _is_binary_change(repo, base_sha, head_sha, path):
            skipped.append({"source_path": path, "reason": "binary"})
            continue

        patch = _git(
            repo,
            "diff",
            "--no-ext-diff",
            "--unified=12",
            base_sha,
            head_sha,
            "--",
            path,
        )
        if not patch:
            continue
        if len(selected) >= max_files:
            degraded = True
            skipped.append({"source_path": path, "reason": "file_count_budget"})
            continue
        if len(patch) > max_file_bytes:
            degraded = True
            skipped.append(
                {
                    "source_path": path,
                    "reason": "per_file_byte_budget",
                    "bytes": len(patch),
                }
            )
            continue
        if total_bytes + len(patch) > max_total_bytes:
            degraded = True
            skipped.append(
                {
                    "source_path": path,
                    "reason": "total_byte_budget",
                    "bytes": len(patch),
                }
            )
            continue

        case_id = f"change-{len(selected) + 1:04d}"
        rel = Path("cases") / f"{case_id}.patch"
        (output / rel).write_bytes(patch)
        selected.append(
            {
                "id": case_id,
                "source_path": path,
                "patch_file": rel.as_posix(),
                "bytes": len(patch),
                "sha256": hashlib.sha256(patch).hexdigest(),
            }
        )
        total_bytes += len(patch)

    if degraded:
        status = "DEGRADED"
    elif selected:
        status = "READY"
    else:
        status = "NO_SIGNAL"

    manifest = {
        "schema_version": 1,
        "status": status,
        "repository": repository,
        "task_id": task_id,
        "base_sha": base_sha,
        "candidate_sha": head_sha,
        "limits": {
            "max_files": max_files,
            "max_total_bytes": max_total_bytes,
            "max_file_bytes": max_file_bytes,
        },
        "selected": selected,
        "skipped": skipped,
        "selected_bytes": total_bytes,
        "changed_path_count": len(paths),
    }
    manifest_path = output / "input-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    manifest["manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--max-files", type=int, default=20)
    parser.add_argument("--max-total-bytes", type=int, default=262144)
    parser.add_argument("--max-file-bytes", type=int, default=65536)
    args = parser.parse_args()

    manifest = build_manifest(
        args.repo,
        args.base,
        args.head,
        args.output,
        args.repository,
        args.task_id,
        max_files=args.max_files,
        max_total_bytes=args.max_total_bytes,
        max_file_bytes=args.max_file_bytes,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
