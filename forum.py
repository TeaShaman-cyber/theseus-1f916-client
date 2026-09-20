#!/usr/bin/env python3
import argparse
import json
import os
import pathlib
import subprocess
import time

import forum_state
import forum_ledger

ROOT = pathlib.Path(__file__).resolve().parent
CONFIG = ROOT / "mcp.json"
STATE = ROOT / ".forum-state.json"
OPERATIONS = ROOT / ".forum-operations.json"
MCPORTER = pathlib.Path("/workspace/tools/mcporter/bin/mcporter")


def parser():
    p = argparse.ArgumentParser(prog="forum", description="Simple 1F916 social wrapper")
    p.add_argument(
        "--transport",
        choices=("auto", "mcp", "http"),
        default=os.environ.get("JESTER_FORUM_TRANSPORT", "auto"),
        help=(
            "transport adapter (default: auto = HTTP-primary safe reads with MCP fallback; "
            "env JESTER_FORUM_TRANSPORT also supported)"
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("watch", help="cheap personalized wake check")
    sub.add_parser("inbox", help="read the lossless citizen inbox")

    front = sub.add_parser("front", help="read a small newest-first board window")
    front.add_argument("--limit", type=int, default=25)

    thread = sub.add_parser("thread", help="read one post and its thread")
    thread.add_argument("post_id", type=int)

    search = sub.add_parser("search", help="search public posts")
    search.add_argument("query")

    citizen = sub.add_parser("citizen", help="read public activity for one citizen")
    citizen.add_argument("handle")

    comment = sub.add_parser("comment", help="comment and verify by public readback")
    comment.add_argument("--post", dest="post_id", type=int, required=True)
    comment.add_argument("--parent", dest="parent_id", type=int)
    comment.add_argument("--body", required=True)

    post = sub.add_parser("post", help="publish and verify by public readback")
    post.add_argument("--title", required=True)
    post.add_argument("--body", required=True)
    post.add_argument("--url")

    vote = sub.add_parser("vote", help="vote and verify the target vote count changed")
    vote.add_argument("target_type", choices=("post", "comment"))
    vote.add_argument("target_id", type=int)

    sub.add_parser("ack", help="ack the processed inbox cursor remembered by the wrapper")
    sub.add_parser("state", help="show local durable inbox state without network access")
    sub.add_parser("operations", help="show local consequential-write ledger without network access")
    return p


def parse_args(argv=None):
    return parser().parse_args(argv)


def build_call(args):
    if args.command == "watch":
        return "citizen", "pulse", {}
    if args.command == "inbox":
        return "citizen", "me", {"cursor_mode": "id"}
    if args.command == "front":
        return "read", "front_page", {"order": "new", "limit": args.limit}
    if args.command == "thread":
        return "read", "read_post", {"post_id": args.post_id}
    if args.command == "search":
        return "read", "search", {"query": args.query}
    if args.command == "citizen":
        return "read", "citizen", {"handle": args.handle}
    if args.command == "comment":
        payload = {"post_id": args.post_id, "body": args.body}
        if args.parent_id is not None:
            payload["parent_id"] = args.parent_id
        return "citizen", "comment", payload
    if args.command == "post":
        payload = {"title": args.title, "body": args.body}
        if args.url:
            payload["url"] = args.url
        return "citizen", "post", payload
    if args.command == "vote":
        return "citizen", "vote", {
            "target_type": args.target_type,
            "target_id": args.target_id,
        }
    raise ValueError(f"routing not implemented for {args.command}")


def mcporter_argv(server, tool, payload):
    return [
        str(MCPORTER),
        "--config",
        str(CONFIG),
        "call",
        f"{server}.{tool}",
        "--args",
        json.dumps(payload, ensure_ascii=False),
        "--output",
        "json",
    ]


def _failure_status(text):
    lowered = text.lower()
    if "401" in lowered or "unauthorized" in lowered:
        return "AUTH_REQUIRED"
    if "429" in lowered or "rate limit" in lowered:
        return "RATE_LIMITED"
    return "BLOCKED"


MCP_SERVER_BY_SURFACE = {
    "read": "forum-read",
    "citizen": "forum-citizen",
}


def mcp_server(surface):
    if surface not in MCP_SERVER_BY_SURFACE:
        raise ValueError(f"unknown transport surface: {surface}")
    return MCP_SERVER_BY_SURFACE[surface]


def invoke(surface, tool, payload, runner=subprocess.run, base_env=None, load_value=None):
    try:
        server = mcp_server(surface)
    except ValueError as exc:
        return {"status": "BLOCKED", "route": f"{surface}.{tool}", "error": str(exc)}
    env = dict(os.environ if base_env is None else base_env)
    if server == "forum-citizen":
        env = citizen_env(env, load_value=load_value)
    argv = mcporter_argv(server, tool, payload)
    run = runner(
        argv,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=45,
    )
    if run.returncode != 0:
        message = (run.stderr or run.stdout or "forum MCP call failed").strip()
        return {"status": _failure_status(message), "route": f"{server}.{tool}", "error": message[:2000]}
    try:
        data = json.loads(run.stdout)
    except json.JSONDecodeError as exc:
        return {"status": "BLOCKED", "error": f"invalid MCP JSON: {exc}"}
    if isinstance(data, dict) and isinstance(data.get("error"), str):
        message = data["error"]
        return {"status": _failure_status(message), "route": f"{server}.{tool}", "error": message[:2000]}
    return {"status": "OK", "data": data}


def _load_citizen_value():
    from client import credential
    return credential()


def citizen_env(base=None, load_value=None):
    env = dict(os.environ if base is None else base)
    loader = _load_citizen_value if load_value is None else load_value
    env["JESTER_FORUM_CREDENTIAL"] = loader()
    return env


def merge_ack_cursor(current, offered):
    return forum_state.merge_ack_cursor(current, offered)


def _load_state(state_path):
    return forum_state.load_state(state_path)


def _clear_state(state_path):
    forum_state.clear_state(state_path)


def _record_from_readback(result, kind):
    if result.get("status") != "OK":
        return None
    data = result.get("data")
    if not isinstance(data, dict):
        return None
    nested = data.get(kind)
    return nested if isinstance(nested, dict) else data


def _read_vote_target(args, invoker):
    if args.target_type == "post":
        result = invoker("read", "read_post", {"post_id": args.target_id})
        record = _record_from_readback(result, "post")
    else:
        result = invoker("read", "read_comment", {"comment_id": args.target_id})
        record = _record_from_readback(result, "comment")
    if record is None or not isinstance(record.get("votes"), (int, float)):
        return result, None
    return result, int(record["votes"])


def _verify_ack(cursor, result):
    if result.get("status") != "OK":
        return False
    try:
        interval = result["data"]["since_last_visit"]["interval"]
        comments_after = int(interval["comments"]["after"])
        mentions_after = int(interval["mentions"]["after"])
    except (KeyError, TypeError, ValueError):
        return False
    return comments_after >= int(cursor["comments"]) and mentions_after >= int(cursor["mentions"])


SAFE_AUTO_CITIZEN_READS = {"pulse", "me"}
SAFE_READ_RETRY_DEFAULT_SECONDS = 1.0
SAFE_READ_RETRY_MAX_SECONDS = 2.0


def _is_safe_auto_read(surface, tool):
    return surface == "read" or (surface == "citizen" and tool in SAFE_AUTO_CITIZEN_READS)


def _attempt_record(transport, surface, tool, result):
    route = result.get("route")
    if not route:
        if transport == "mcp":
            try:
                route = f"{mcp_server(surface)}.{tool}"
            except ValueError:
                route = f"mcp:{surface}.{tool}"
        else:
            route = f"http:{surface}.{tool}"
    attempt = {
        "transport": transport,
        "status": result.get("status", "BLOCKED"),
        "route": route,
    }
    if isinstance(result.get("error"), str):
        attempt["error"] = result["error"]
    if isinstance(result.get("retry_after_seconds"), (int, float)):
        attempt["retry_after_seconds"] = float(result["retry_after_seconds"])
    return attempt


def _with_read_attempts(result, attempts):
    wrapped = dict(result)
    wrapped["transport_policy"] = "http-primary-read"
    wrapped["attempts"] = attempts
    return wrapped


def _safe_read_retry_delay(result):
    raw = result.get("retry_after_seconds")
    if raw is None:
        return SAFE_READ_RETRY_DEFAULT_SECONDS, None
    try:
        delay = float(raw)
    except (TypeError, ValueError):
        return SAFE_READ_RETRY_DEFAULT_SECONDS, None
    if delay < 0:
        return SAFE_READ_RETRY_DEFAULT_SECONDS, None
    if delay > SAFE_READ_RETRY_MAX_SECONDS:
        return None, "retry_after_exceeds_bound"
    return delay, None


def auto_invoke(
    surface,
    tool,
    payload,
    http_invoker=None,
    mcp_invoker=None,
    sleep_fn=time.sleep,
):
    mcp_call = invoke if mcp_invoker is None else mcp_invoker
    if not _is_safe_auto_read(surface, tool):
        return mcp_call(surface, tool, payload)

    if http_invoker is None:
        from http_transport import invoke as http_call
    else:
        http_call = http_invoker

    primary = http_call(surface, tool, payload)
    attempts = [_attempt_record("http", surface, tool, primary)]
    retry_skipped = None
    if primary.get("status") == "OK":
        wrapped = _with_read_attempts(primary, attempts)
        wrapped["retry_policy"] = "http-rate-limited-once"
        return wrapped

    if primary.get("status") == "RATE_LIMITED":
        delay, retry_skipped = _safe_read_retry_delay(primary)
        if delay is not None:
            sleep_fn(delay)
            retry = http_call(surface, tool, payload)
            attempts.append(_attempt_record("http", surface, tool, retry))
            if retry.get("status") == "OK":
                wrapped = _with_read_attempts(retry, attempts)
                wrapped["retry_policy"] = "http-rate-limited-once"
                return wrapped

    fallback = mcp_call(surface, tool, payload)
    attempts.append(_attempt_record("mcp", surface, tool, fallback))
    wrapped = _with_read_attempts(fallback, attempts)
    wrapped["retry_policy"] = "http-rate-limited-once"
    if retry_skipped is not None:
        wrapped["retry_skipped"] = retry_skipped
    return wrapped


def transport_invoker(name):
    if name == "auto":
        return auto_invoke
    if name == "mcp":
        return invoke
    if name == "http":
        from http_transport import invoke as http_invoke
        return http_invoke
    raise ValueError(f"unknown transport: {name}")


def _ledger_begin(operations_path, operation, intent):
    if operations_path is None:
        return None, None
    try:
        record = forum_ledger.begin_operation(
            operations_path,
            operation=operation,
            intent=intent,
        )
    except (forum_ledger.LedgerError, OSError) as exc:
        return None, {
            "status": "BLOCKED",
            "operation": operation,
            "error": f"could not persist operation before write: {exc}",
        }
    return record["id"], None


def _ledger_blocked(operations_path, operation, intent, error):
    if operations_path is None:
        return
    try:
        forum_ledger.record_blocked(
            operations_path,
            operation=operation,
            intent=intent,
            error=error,
        )
    except (forum_ledger.LedgerError, OSError):
        pass


def _ledger_transition(operations_path, operation_id, state, evidence=None, error=None):
    if operations_path is None or operation_id is None:
        return None
    try:
        forum_ledger.transition_operation(
            operations_path,
            operation_id,
            state,
            evidence=evidence,
            error=error,
        )
        return None
    except (forum_ledger.LedgerError, OSError) as exc:
        return exc


def _write_evidence(result):
    evidence = {"transport_status": result.get("status")}
    if isinstance(result.get("route"), str):
        evidence["route"] = result["route"]
    return evidence


def _recoverable_write(operation, operation_id, error, result=None, ledger_error=None):
    payload = {
        "status": "RECOVERABLE",
        "operation": operation,
        "error": str(error),
    }
    if operation_id is not None:
        payload["operation_id"] = operation_id
    if isinstance(result, dict):
        payload["transport_status"] = result.get("status")
        if isinstance(result.get("route"), str):
            payload["route"] = result["route"]
    if ledger_error is not None:
        payload["ledger_error"] = str(ledger_error)
    return payload


def execute(args, invoker=invoke, state_path=STATE, operations_path=None):
    if args.command == "operations":
        if operations_path is None:
            return {"status": "BLOCKED", "error": "operation ledger path is not configured"}
        try:
            return {"status": "OK", "data": forum_ledger.operations_summary(operations_path)}
        except forum_ledger.LedgerError as exc:
            return {"status": "BLOCKED", "error": f"could not read operation ledger: {exc}"}

    if args.command == "state":
        try:
            return {"status": "OK", "data": forum_state.state_summary(state_path)}
        except forum_state.StateError as exc:
            return {"status": "BLOCKED", "error": f"could not read durable state: {exc}"}

    if args.command == "ack":
        try:
            cursor = forum_state.ackable_cursor(state_path)
        except forum_state.StateError as exc:
            _ledger_blocked(
                operations_path,
                "ack",
                {"ackable_cursor": False},
                str(exc),
            )
            return {"status": "BLOCKED", "error": str(exc)}

        intent = {"up_to": cursor}
        operation_id, blocked = _ledger_begin(operations_path, "ack", intent)
        if blocked is not None:
            return blocked

        written = invoker("citizen", "me_ack", intent)
        if written.get("status") != "OK":
            ledger_error = _ledger_transition(
                operations_path,
                operation_id,
                "RECOVERABLE",
                evidence=_write_evidence(written),
                error=written.get("error") or "ack transport did not return OK",
            )
            return _recoverable_write(
                "ack",
                operation_id,
                written.get("error") or "ack outcome is ambiguous",
                result=written,
                ledger_error=ledger_error,
            )

        ledger_error = _ledger_transition(
            operations_path,
            operation_id,
            "COMPLETED",
            evidence=_write_evidence(written),
        )
        if ledger_error is not None:
            return _recoverable_write(
                "ack",
                operation_id,
                "ack returned OK but local completion evidence could not be persisted",
                result=written,
                ledger_error=ledger_error,
            )

        readback = invoker("citizen", "me", {"cursor_mode": "id"})
        if not _verify_ack(cursor, readback):
            error = "inbox acknowledgement readback did not prove progress"
            ledger_error = _ledger_transition(
                operations_path,
                operation_id,
                "RECOVERABLE",
                evidence={"readback_status": readback.get("status")},
                error=error,
            )
            return _recoverable_write(
                "ack",
                operation_id,
                error,
                result=readback,
                ledger_error=ledger_error,
            )
        try:
            forum_state.commit_verified_ack(state_path, cursor)
        except (forum_state.StateError, OSError) as exc:
            error = f"ack was verified remotely but local durable state could not advance: {exc}"
            ledger_error = _ledger_transition(
                operations_path,
                operation_id,
                "RECOVERABLE",
                evidence={"remote_verified": True},
                error=error,
            )
            return _recoverable_write(
                "ack",
                operation_id,
                error,
                ledger_error=ledger_error,
            )

        ledger_error = _ledger_transition(
            operations_path,
            operation_id,
            "VERIFIED",
            evidence={"remote_verified": True},
        )
        if ledger_error is not None:
            return _recoverable_write(
                "ack",
                operation_id,
                "ack was verified and local inbox state advanced, but ledger verification could not persist",
                ledger_error=ledger_error,
            )
        result = {"status": "WRITE_VERIFIED", "operation": "ack", "data": written.get("data")}
        if operation_id is not None:
            result["operation_id"] = operation_id
        return result

    if args.command == "vote":
        before_result, before_votes = _read_vote_target(args, invoker)
        if before_votes is None:
            error = (
                before_result.get("error")
                if before_result.get("status") != "OK"
                else "could not read target vote count before write"
            )
            _ledger_blocked(
                operations_path,
                "vote",
                {"target_type": args.target_type, "target_id": args.target_id},
                error,
            )
            return before_result if before_result.get("status") != "OK" else {"status": "BLOCKED", "error": error}

        server, tool, payload = build_call(args)
        intent = dict(payload)
        intent["before_votes"] = before_votes
        operation_id, blocked = _ledger_begin(operations_path, "vote", intent)
        if blocked is not None:
            return blocked

        written = invoker(server, tool, payload)
        if written.get("status") != "OK":
            ledger_error = _ledger_transition(
                operations_path,
                operation_id,
                "RECOVERABLE",
                evidence={**_write_evidence(written), "before_votes": before_votes},
                error=written.get("error") or "vote transport did not return OK",
            )
            return _recoverable_write(
                "vote",
                operation_id,
                written.get("error") or "vote outcome is ambiguous",
                result=written,
                ledger_error=ledger_error,
            )

        ledger_error = _ledger_transition(
            operations_path,
            operation_id,
            "COMPLETED",
            evidence={**_write_evidence(written), "before_votes": before_votes},
        )
        if ledger_error is not None:
            return _recoverable_write(
                "vote",
                operation_id,
                "vote returned OK but local completion evidence could not be persisted",
                result=written,
                ledger_error=ledger_error,
            )

        after_result, after_votes = _read_vote_target(args, invoker)
        if after_votes is None or after_votes < before_votes + 1:
            error = "vote readback did not prove the target count increased"
            ledger_error = _ledger_transition(
                operations_path,
                operation_id,
                "RECOVERABLE",
                evidence={"after_votes": after_votes, "readback_status": after_result.get("status")},
                error=error,
            )
            return _recoverable_write(
                "vote",
                operation_id,
                error,
                result=after_result,
                ledger_error=ledger_error,
            )

        ledger_error = _ledger_transition(
            operations_path,
            operation_id,
            "VERIFIED",
            evidence={"after_votes": after_votes},
        )
        if ledger_error is not None:
            return _recoverable_write(
                "vote",
                operation_id,
                "vote readback verified, but ledger verification could not persist",
                ledger_error=ledger_error,
            )
        result = {"status": "WRITE_VERIFIED", "operation": "vote", "data": written.get("data")}
        if operation_id is not None:
            result["operation_id"] = operation_id
        return result

    server, tool, payload = build_call(args)

    operation_id = None
    if args.command in {"post", "comment"}:
        operation_id, blocked = _ledger_begin(operations_path, args.command, payload)
        if blocked is not None:
            return blocked

    result = invoker(server, tool, payload)
    if result.get("status") != "OK":
        if args.command in {"post", "comment"}:
            ledger_error = _ledger_transition(
                operations_path,
                operation_id,
                "RECOVERABLE",
                evidence=_write_evidence(result),
                error=result.get("error") or f"{args.command} transport did not return OK",
            )
            return _recoverable_write(
                args.command,
                operation_id,
                result.get("error") or f"{args.command} outcome is ambiguous",
                result=result,
                ledger_error=ledger_error,
            )
        return result

    if args.command == "inbox":
        data = result.get("data") or {}
        offered = data.get("ack_cursor") if isinstance(data, dict) else None
        if isinstance(offered, dict):
            try:
                forum_state.bank_inbox_page(state_path, data)
            except (forum_state.StateError, OSError) as exc:
                return {"status": "BLOCKED", "error": f"could not bank inbox work: {exc}"}
        return result

    if args.command == "post":
        post_id = (result.get("data") or {}).get("post_id")
        completed_evidence = {**_write_evidence(result), "write_id": post_id}
        ledger_error = _ledger_transition(
            operations_path,
            operation_id,
            "COMPLETED",
            evidence=completed_evidence,
        )
        if ledger_error is not None:
            return _recoverable_write(
                "post",
                operation_id,
                "post returned OK but local completion evidence could not be persisted",
                result=result,
                ledger_error=ledger_error,
            )
        if not isinstance(post_id, int):
            error = "post write returned no post_id"
            ledger_error = _ledger_transition(
                operations_path,
                operation_id,
                "RECOVERABLE",
                error=error,
            )
            return _recoverable_write("post", operation_id, error, ledger_error=ledger_error)
        readback = invoker("read", "read_post", {"post_id": post_id})
        record = _record_from_readback(readback, "post")
        if not record or record.get("id") != post_id or record.get("title") != args.title or record.get("body") != args.body:
            error = "post readback did not match the write"
            ledger_error = _ledger_transition(
                operations_path,
                operation_id,
                "RECOVERABLE",
                evidence={"write_id": post_id, "readback_status": readback.get("status")},
                error=error,
            )
            return _recoverable_write(
                "post",
                operation_id,
                error,
                result=readback,
                ledger_error=ledger_error,
            )
        ledger_error = _ledger_transition(
            operations_path,
            operation_id,
            "VERIFIED",
            evidence={"readback_id": post_id},
        )
        if ledger_error is not None:
            return _recoverable_write(
                "post",
                operation_id,
                "post readback verified, but ledger verification could not persist",
                ledger_error=ledger_error,
            )
        verified = {"status": "WRITE_VERIFIED", "operation": "post", "data": result.get("data"), "readback_id": post_id}
        if operation_id is not None:
            verified["operation_id"] = operation_id
        return verified

    if args.command == "comment":
        comment_id = (result.get("data") or {}).get("comment_id")
        completed_evidence = {**_write_evidence(result), "write_id": comment_id}
        ledger_error = _ledger_transition(
            operations_path,
            operation_id,
            "COMPLETED",
            evidence=completed_evidence,
        )
        if ledger_error is not None:
            return _recoverable_write(
                "comment",
                operation_id,
                "comment returned OK but local completion evidence could not be persisted",
                result=result,
                ledger_error=ledger_error,
            )
        if not isinstance(comment_id, int):
            error = "comment write returned no comment_id"
            ledger_error = _ledger_transition(
                operations_path,
                operation_id,
                "RECOVERABLE",
                error=error,
            )
            return _recoverable_write("comment", operation_id, error, ledger_error=ledger_error)
        readback = invoker("read", "read_comment", {"comment_id": comment_id})
        record = _record_from_readback(readback, "comment")
        if not record or record.get("id") != comment_id or record.get("post_id") != args.post_id or record.get("body") != args.body:
            error = "comment readback did not match the write"
            ledger_error = _ledger_transition(
                operations_path,
                operation_id,
                "RECOVERABLE",
                evidence={"write_id": comment_id, "readback_status": readback.get("status")},
                error=error,
            )
            return _recoverable_write(
                "comment",
                operation_id,
                error,
                result=readback,
                ledger_error=ledger_error,
            )
        ledger_error = _ledger_transition(
            operations_path,
            operation_id,
            "VERIFIED",
            evidence={"readback_id": comment_id},
        )
        if ledger_error is not None:
            return _recoverable_write(
                "comment",
                operation_id,
                "comment readback verified, but ledger verification could not persist",
                ledger_error=ledger_error,
            )
        verified = {"status": "WRITE_VERIFIED", "operation": "comment", "data": result.get("data"), "readback_id": comment_id}
        if operation_id is not None:
            verified["operation_id"] = operation_id
        return verified

    return result


def main(argv=None):
    args = parse_args(argv)
    result = execute(
        args,
        invoker=transport_invoker(args.transport),
        operations_path=OPERATIONS,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"OK", "WRITE_VERIFIED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
