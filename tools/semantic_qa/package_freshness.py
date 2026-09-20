from __future__ import annotations

import argparse
import json
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def evaluate_freshness(
    *,
    now: datetime,
    built_at: str | None,
    expires_at: str | None,
    max_age_days: int,
    expiry_warning_days: int,
    pinned_git_sha: str | None = None,
    current_git_sha: str | None = None,
    pinned_hf_revision: str | None = None,
    current_hf_revision: str | None = None,
    currentness_errors: list[str] | None = None,
) -> dict[str, Any]:
    now = now.astimezone(timezone.utc)
    reasons: list[str] = []
    warnings: list[str] = []
    errors = list(currentness_errors or [])

    built = _parse_time(built_at)
    expires = _parse_time(expires_at)

    if built is not None:
        age_days = (now - built).total_seconds() / 86400
        if age_days > max_age_days:
            reasons.append(f"package_age_days={age_days:.1f}>{max_age_days}")
    else:
        age_days = None
        warnings.append("package build timestamp unavailable")

    if expires is not None:
        expiry_days = (expires - now).total_seconds() / 86400
        if expiry_days <= expiry_warning_days:
            reasons.append(
                f"artifact_expiry_days={expiry_days:.1f}<={expiry_warning_days}"
            )
    else:
        expiry_days = None
        warnings.append("artifact expiry timestamp unavailable")

    if pinned_git_sha and current_git_sha and pinned_git_sha != current_git_sha:
        reasons.append(
            f"upstream_git_moved:{pinned_git_sha[:12]}->{current_git_sha[:12]}"
        )

    if (
        pinned_hf_revision
        and current_hf_revision
        and pinned_hf_revision != current_hf_revision
    ):
        reasons.append(
            "upstream_model_moved:"
            f"{pinned_hf_revision[:12]}->{current_hf_revision[:12]}"
        )

    if reasons:
        status = "STALE_AVAILABLE"
    elif errors:
        status = "CURRENTNESS_UNKNOWN"
    else:
        status = "CURRENT"

    return {
        "status": status,
        "reasons": reasons,
        "warnings": warnings,
        "currentness_errors": errors,
        "age_days": age_days,
        "expiry_days": expiry_days,
    }


def _git_head(url: str, ref: str) -> str:
    cp = subprocess.run(
        ["git", "ls-remote", url, ref],
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    )
    line = cp.stdout.strip().splitlines()
    if not line:
        raise RuntimeError(f"no git ref returned for {url} {ref}")
    return line[0].split()[0]


def _hf_head(repo: str) -> str:
    url = f"https://huggingface.co/api/models/{repo}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "theseus-semantic-qa-freshness/1"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.load(response)
    sha = payload.get("sha")
    if not isinstance(sha, str) or not sha:
        raise RuntimeError(f"Hugging Face response for {repo} has no sha")
    return sha


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--built-at")
    parser.add_argument("--expires-at")
    parser.add_argument("--max-age-days", type=int, default=30)
    parser.add_argument("--expiry-warning-days", type=int, default=14)
    parser.add_argument("--git-url")
    parser.add_argument("--git-ref", default="refs/heads/main")
    parser.add_argument("--pinned-git-sha")
    parser.add_argument("--hf-repo")
    parser.add_argument("--pinned-hf-revision")
    args = parser.parse_args()

    errors: list[str] = []
    current_git_sha = None
    current_hf_revision = None

    if args.git_url and args.pinned_git_sha:
        try:
            current_git_sha = _git_head(args.git_url, args.git_ref)
        except Exception as exc:
            errors.append(f"git_currentness:{type(exc).__name__}:{exc}")

    if args.hf_repo and args.pinned_hf_revision:
        try:
            current_hf_revision = _hf_head(args.hf_repo)
        except Exception as exc:
            errors.append(f"hf_currentness:{type(exc).__name__}:{exc}")

    result = evaluate_freshness(
        now=datetime.now(timezone.utc),
        built_at=args.built_at,
        expires_at=args.expires_at,
        max_age_days=args.max_age_days,
        expiry_warning_days=args.expiry_warning_days,
        pinned_git_sha=args.pinned_git_sha,
        current_git_sha=current_git_sha,
        pinned_hf_revision=args.pinned_hf_revision,
        current_hf_revision=current_hf_revision,
        currentness_errors=errors,
    )
    receipt = {
        "schema_version": 1,
        "tool": args.tool,
        **result,
        "pinned_git_sha": args.pinned_git_sha,
        "current_git_sha": current_git_sha,
        "pinned_hf_revision": args.pinned_hf_revision,
        "current_hf_revision": current_hf_revision,
        "blocking": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
