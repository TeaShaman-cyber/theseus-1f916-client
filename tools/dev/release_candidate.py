#!/usr/bin/env python3
import argparse
import json
import pathlib
import re
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_REPO = "TeaShaman-cyber/theseus-1f916-client"
REVIEW_DISPOSITION = "NO_SUBSTANTIVE_UNRESOLVED_FINDINGS"


class GateError(RuntimeError):
    pass


class CommandRunner:
    def __init__(self, root=ROOT):
        self.root = pathlib.Path(root)

    def run(self, argv):
        completed = subprocess.run(
            list(argv),
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise GateError(
                f"command failed ({completed.returncode}): {' '.join(argv)}"
                + (f": {detail[:2000]}" if detail else "")
            )
        return completed.stdout


def _require_sha(value, label):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise GateError(f"{label} must be a full lowercase 40-hex SHA")
    return value


def _single_sha_marker(log, key, expected_sha):
    values = set(re.findall(rf"{re.escape(key)}=([0-9a-f]{{40}})", log))
    if not values:
        raise GateError(f"hosted log missing {key}")
    if values != {expected_sha}:
        raise GateError(
            f"hosted {key} mismatch: expected {expected_sha}, observed {sorted(values)}"
        )
    return expected_sha


def validate_hosted_receipt(kind, metadata, log, expected_sha, run_id):
    _require_sha(expected_sha, "expected SHA")
    if not isinstance(metadata, dict):
        raise GateError(f"{kind} run metadata must be an object")
    if metadata.get("status") != "completed" or metadata.get("conclusion") != "success":
        raise GateError(
            f"{kind} hosted run is not successful: "
            f"status={metadata.get('status')!r} conclusion={metadata.get('conclusion')!r}"
        )
    if metadata.get("headSha") != expected_sha:
        raise GateError(
            f"{kind} run head mismatch: expected {expected_sha}, observed {metadata.get('headSha')!r}"
        )
    _single_sha_marker(log, "verification_source_sha", expected_sha)
    _single_sha_marker(log, "verification_checkout_sha", expected_sha)

    receipt = {
        "run_id": int(run_id),
        "url": metadata.get("url"),
        "workflow": metadata.get("workflowName"),
        "event": metadata.get("event"),
        "head_sha": expected_sha,
        "source_sha": expected_sha,
        "checkout_sha": expected_sha,
        "conclusion": "success",
    }
    if kind == "canonical":
        if "DEV_CHECK_PASS" not in log:
            raise GateError("canonical hosted log missing DEV_CHECK_PASS")
        receipt["marker"] = "DEV_CHECK_PASS"
        return receipt

    if kind != "property":
        raise GateError(f"unsupported hosted receipt kind: {kind}")
    match = re.search(
        r"PROPERTY_TEST_PASS suite=([^\s]+) max_examples=(\d+) properties=(\d+)",
        log,
    )
    if match is None:
        raise GateError("property hosted log missing PROPERTY_TEST_PASS receipt")
    runtime = re.search(r"PROPERTY_TEST_RUNTIME seconds=(\d+) exit=(\d+)", log)
    if runtime is None or int(runtime.group(2)) != 0:
        raise GateError("property hosted log missing successful runtime receipt")
    receipt.update(
        {
            "marker": "PROPERTY_TEST_PASS",
            "suite": match.group(1),
            "max_examples": int(match.group(2)),
            "properties": int(match.group(3)),
            "runtime_seconds": int(runtime.group(1)),
        }
    )
    if receipt["max_examples"] < 1 or receipt["properties"] < 1:
        raise GateError("property receipt must report positive example/property counts")
    return receipt


def validate_review_receipt(raw, expected_sha):
    if not isinstance(raw, dict):
        raise GateError("review receipt must be a JSON object")
    if raw.get("schema_version") != 1:
        raise GateError("review receipt schema_version must be 1")
    if raw.get("candidate_sha") != expected_sha:
        raise GateError("review receipt candidate_sha does not match exact candidate")
    if raw.get("disposition") != REVIEW_DISPOSITION:
        raise GateError(
            f"review disposition must be {REVIEW_DISPOSITION}"
        )
    evidence = raw.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise GateError("review receipt must contain evidence")
    normalized = []
    independent_count = 0
    for index, item in enumerate(evidence):
        if not isinstance(item, dict):
            raise GateError(f"review evidence[{index}] must be an object")
        channel = item.get("channel")
        ref = item.get("ref")
        independent = item.get("independent") is True
        if not isinstance(channel, str) or not channel.strip():
            raise GateError(f"review evidence[{index}].channel must be non-empty")
        if not isinstance(ref, str) or not ref.strip():
            raise GateError(f"review evidence[{index}].ref must be non-empty")
        if independent:
            independent_count += 1
        normalized.append(
            {
                "channel": channel.strip(),
                "ref": ref.strip(),
                "independent": independent,
            }
        )
    if independent_count < 1:
        raise GateError("review receipt requires at least one independent evidence channel")
    return {
        "schema_version": 1,
        "candidate_sha": expected_sha,
        "disposition": REVIEW_DISPOSITION,
        "evidence": normalized,
        "independent_evidence_count": independent_count,
    }


def _read_json(path):
    try:
        return json.loads(pathlib.Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError(f"could not read review receipt: {exc}") from exc


def _load_run(runner, repo, run_id, kind, expected_sha):
    metadata_text = runner.run(
        [
            "gh",
            "run",
            "view",
            str(run_id),
            "-R",
            repo,
            "--json",
            "status,conclusion,headSha,event,workflowName,url",
        ]
    )
    try:
        metadata = json.loads(metadata_text)
    except json.JSONDecodeError as exc:
        raise GateError(f"{kind} run metadata is not JSON") from exc
    log = runner.run(["gh", "run", "view", str(run_id), "-R", repo, "--log"])
    return validate_hosted_receipt(kind, metadata, log, expected_sha, run_id)


def _clean_status(runner):
    status = runner.run(["git", "status", "--porcelain=v1", "--untracked-files=all"])
    if status.strip():
        raise GateError("worktree/index must be clean for RC gate")


def evaluate_candidate(args, runner=None, root=ROOT):
    runner = CommandRunner(root) if runner is None else runner
    root = pathlib.Path(root)
    expected_sha = _require_sha(args.expected_sha, "expected SHA")

    runner.run(["git", "fetch", "origin", "main", "--prune"])
    branch = runner.run(["git", "branch", "--show-current"]).strip()
    if branch != "main":
        raise GateError(f"RC gate requires branch main, observed {branch!r}")
    upstream = runner.run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]
    ).strip()
    if upstream != "origin/main":
        raise GateError(f"main upstream must be origin/main, observed {upstream!r}")
    head = _require_sha(runner.run(["git", "rev-parse", "HEAD"]).strip(), "HEAD")
    remote = _require_sha(
        runner.run(["git", "rev-parse", "origin/main"]).strip(), "origin/main"
    )
    if head != expected_sha or remote != expected_sha:
        raise GateError(
            f"candidate identity mismatch: expected={expected_sha} HEAD={head} origin/main={remote}"
        )
    _clean_status(runner)

    version = (root / "VERSION").read_text().strip()
    if version != args.expected_version:
        raise GateError(
            f"VERSION mismatch: expected {args.expected_version!r}, observed {version!r}"
        )

    conformance = runner.run(
        ["python3", "-m", "unittest", "tests.test_transport_conformance", "-v"]
    )
    runner.run(["bash", "tools/dev/check"])
    _clean_status(runner)

    canonical = _load_run(
        runner, args.repo, args.canonical_run, "canonical", expected_sha
    )
    property_receipt = _load_run(
        runner, args.repo, args.property_run, "property", expected_sha
    )
    review = validate_review_receipt(
        _read_json(args.review_receipt), expected_sha
    )

    conformance_match = re.search(r"Ran (\d+) tests", conformance)
    conformance_tests = (
        int(conformance_match.group(1)) if conformance_match is not None else None
    )

    return {
        "schema_version": 1,
        "status": "RC_READY",
        "promotion_authorized": False,
        "candidate_sha": expected_sha,
        "version": version,
        "branch": "main",
        "remote_ref": "origin/main",
        "worktree_clean": True,
        "local": {
            "canonical": "DEV_CHECK_PASS",
            "conformance": "PASS",
            "conformance_tests": conformance_tests,
        },
        "hosted": {
            "canonical": canonical,
            "property": property_receipt,
        },
        "review": review,
        "known_advisory_debt": ["issue #22 mutation survivor classification"],
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Verify exact-head v1 release-candidate readiness without promoting a release."
    )
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--canonical-run", required=True, type=int)
    parser.add_argument("--property-run", required=True, type=int)
    parser.add_argument("--review-receipt", required=True)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--receipt")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        receipt = evaluate_candidate(args)
        payload = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
        if args.receipt:
            destination = pathlib.Path(args.receipt).resolve()
            try:
                destination.relative_to(ROOT.resolve())
            except ValueError:
                pass
            else:
                raise GateError("RC receipt path must be outside the repository worktree")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(payload)
        sys.stdout.write(payload)
        return 0
    except GateError as exc:
        print(f"RC_BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
