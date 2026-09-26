#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import time

import forum_state
import forum_ledger
import forum_liveness
from client import CLIENT_VERSION

ROOT = pathlib.Path(__file__).resolve().parent
CONFIG = ROOT / "mcp.json"
STATE = ROOT / ".forum-state.json"
OPERATIONS = ROOT / ".forum-operations.json"
LIVENESS = ROOT / ".forum-liveness.json"
MCPORTER = pathlib.Path("/workspace/tools/mcporter/bin/mcporter")


def parser():
    p = argparse.ArgumentParser(prog="forum", description="Simple 1F916 social wrapper")
    p.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {CLIENT_VERSION}",
    )
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
    citizen.add_argument("handle", nargs="?")
    citizen.add_argument(
        "--id",
        dest="citizen_id",
        type=int,
        help="resolve one numeric citizen id through the live complete census",
    )

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
    reconcile = sub.add_parser("reconcile", help="reconcile one unresolved write using read-only evidence")
    reconcile.add_argument("operation_id")
    return p


def parse_args(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.command == "citizen":
        has_handle = isinstance(args.handle, str) and bool(args.handle)
        has_id = args.citizen_id is not None
        if has_handle == has_id:
            p.error("citizen requires exactly one of HANDLE or --id CITIZEN_ID")
    return args


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
        if getattr(args, "citizen_id", None) is not None:
            raise ValueError("numeric citizen id must be resolved before exact citizen lookup")
        if not isinstance(args.handle, str) or not args.handle:
            raise ValueError("citizen requires HANDLE or --id CITIZEN_ID")
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


def _with_search_completeness(result):
    if result.get("status") != "OK":
        return result
    data = result.get("data")
    if not isinstance(data, dict):
        return result

    normalized = dict(result)
    normalized_data = dict(data)
    has_more = data.get("has_more")
    if isinstance(has_more, bool):
        completeness = {
            "status": "TRUNCATED" if has_more else "COMPLETE",
            "evidence": "server_has_more",
        }
        if has_more:
            completeness["action"] = "narrow_query_or_raise_limit"
            max_limit = data.get("max_limit")
            if isinstance(max_limit, int) and not isinstance(max_limit, bool):
                completeness["max_limit"] = max_limit
    else:
        completeness = {
            "status": "UNKNOWN",
            "evidence": "truncation_signal_unavailable",
        }
    normalized_data["completeness"] = completeness
    normalized["data"] = normalized_data
    return normalized


def _thread_cursor_key(value):
    if not isinstance(value, str):
        return None
    created_at_raw, separator, comment_id_raw = value.partition(":")
    if separator != ":" or not created_at_raw or not comment_id_raw:
        return None
    try:
        created_at = int(created_at_raw)
        comment_id = int(comment_id_raw)
    except ValueError:
        return None
    if created_at < 0 or comment_id < 0:
        return None
    return created_at, comment_id


def _thread_page_provenance(result, since):
    receipt = {"since": since, "status": result.get("status")}
    for key in ("route", "transport_policy", "retry_policy", "observer_scope", "attempts"):
        if key in result:
            receipt[key] = result[key]
    return receipt


def _thread_progress(post_id, pages_checked, comments_collected, expected_total, page_provenance, coverage_complete):
    return {
        "post_id": post_id,
        "pages_checked": pages_checked,
        "comments_collected": comments_collected,
        "expected_total": expected_total,
        "coverage_complete": coverage_complete,
        "page_provenance": page_provenance,
    }


def _blocked_thread(post_id, message, pages_checked, comments, expected_total, page_provenance):
    return {
        "status": "BLOCKED",
        "error": message,
        "thread_progress": _thread_progress(
            post_id,
            pages_checked,
            len(comments),
            expected_total,
            page_provenance,
            False,
        ),
    }


def _read_complete_thread(post_id, invoker):
    since = None
    prior_cursor_key = None
    seen_cursors = set()
    seen_comment_ids = set()
    comments = []
    page_provenance = []
    pages_checked = 0
    expected_total = None
    first_result = None
    first_data = None

    while True:
        payload = {"post_id": post_id}
        if since is not None:
            payload["since"] = since
        page = invoker("read", "read_post", payload)
        pages_checked += 1
        page_provenance.append(_thread_page_provenance(page, since))

        if page.get("status") != "OK":
            failed = dict(page)
            failed["thread_progress"] = _thread_progress(
                post_id,
                pages_checked,
                len(comments),
                expected_total,
                page_provenance,
                False,
            )
            return failed

        data = page.get("data")
        if not isinstance(data, dict):
            return _blocked_thread(
                post_id,
                "thread page is not an object",
                pages_checked,
                comments,
                expected_total,
                page_provenance,
            )

        post = data.get("post")
        if first_data is None:
            if not isinstance(post, dict) or post.get("id") != post_id:
                return _blocked_thread(
                    post_id,
                    "thread first page does not identify the requested post",
                    pages_checked,
                    comments,
                    expected_total,
                    page_provenance,
                )
            first_result = dict(page)
            first_data = dict(data)
        elif isinstance(post, dict) and post.get("id") != post_id:
            return _blocked_thread(
                post_id,
                "thread page changed the requested post identity",
                pages_checked,
                comments,
                expected_total,
                page_provenance,
            )

        rows = data.get("comments")
        returned = data.get("comments_returned")
        total = data.get("comments_total")
        has_more = data.get("has_more")
        if not isinstance(rows, list):
            return _blocked_thread(
                post_id,
                "thread page has no comments list",
                pages_checked,
                comments,
                expected_total,
                page_provenance,
            )
        if isinstance(returned, bool) or not isinstance(returned, int) or returned != len(rows):
            return _blocked_thread(
                post_id,
                "thread comments_returned does not match page rows",
                pages_checked,
                comments,
                expected_total,
                page_provenance,
            )
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            return _blocked_thread(
                post_id,
                "thread comments_total is not a non-negative integer",
                pages_checked,
                comments,
                expected_total,
                page_provenance,
            )
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            return _blocked_thread(
                post_id,
                "thread comments_total changed during pagination; retry required",
                pages_checked,
                comments,
                expected_total,
                page_provenance,
            )
        if not isinstance(has_more, bool):
            return _blocked_thread(
                post_id,
                "thread has_more is not boolean",
                pages_checked,
                comments,
                expected_total,
                page_provenance,
            )

        for row in rows:
            if not isinstance(row, dict):
                return _blocked_thread(
                    post_id,
                    "thread contains a non-object comment",
                    pages_checked,
                    comments,
                    expected_total,
                    page_provenance,
                )
            comment_id = row.get("id")
            if isinstance(comment_id, bool) or not isinstance(comment_id, int):
                return _blocked_thread(
                    post_id,
                    "thread contains a comment without an integer id",
                    pages_checked,
                    comments,
                    expected_total,
                    page_provenance,
                )
            if comment_id in seen_comment_ids:
                return _blocked_thread(
                    post_id,
                    "thread pagination returned a duplicate comment id",
                    pages_checked,
                    comments,
                    expected_total,
                    page_provenance,
                )
            seen_comment_ids.add(comment_id)
            comments.append(row)

        if len(comments) > expected_total:
            return _blocked_thread(
                post_id,
                "thread pagination exceeded comments_total",
                pages_checked,
                comments,
                expected_total,
                page_provenance,
            )

        if not has_more:
            if len(comments) != expected_total:
                return _blocked_thread(
                    post_id,
                    "thread ended without proving complete comment coverage",
                    pages_checked,
                    comments,
                    expected_total,
                    page_provenance,
                )
            final_data = dict(first_data)
            final_data["comments"] = comments
            final_data["comments_returned"] = len(comments)
            final_data["comments_total"] = expected_total
            final_data["has_more"] = False
            final_data.pop("next_since", None)
            final_data["thread_complete"] = True
            completed = dict(first_result)
            completed["data"] = final_data
            completed["thread_progress"] = _thread_progress(
                post_id,
                pages_checked,
                len(comments),
                expected_total,
                page_provenance,
                True,
            )
            return completed

        if not rows:
            return _blocked_thread(
                post_id,
                "thread has_more page is empty",
                pages_checked,
                comments,
                expected_total,
                page_provenance,
            )
        next_since = data.get("next_since")
        cursor_key = _thread_cursor_key(next_since)
        if (
            cursor_key is None
            or next_since in seen_cursors
            or (prior_cursor_key is not None and cursor_key <= prior_cursor_key)
        ):
            return _blocked_thread(
                post_id,
                "thread pagination stalled or returned an invalid cursor",
                pages_checked,
                comments,
                expected_total,
                page_provenance,
            )
        seen_cursors.add(next_since)
        prior_cursor_key = cursor_key
        since = next_since


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
RATE_LIMIT_SCOPE_SHARED_OR_UNKNOWN = "shared_or_unknown_edge"
RATE_LIMIT_SCOPE_INDEPENDENT_PEER = "independent_peer"
RATE_LIMIT_BACKOFF_DEFAULT_SECONDS = 60.0


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


def _rate_limit_backoff_seconds(result):
    raw = result.get("retry_after_seconds")
    try:
        delay = float(raw)
    except (TypeError, ValueError):
        delay = RATE_LIMIT_BACKOFF_DEFAULT_SECONDS
    if delay < 0:
        delay = RATE_LIMIT_BACKOFF_DEFAULT_SECONDS
    return max(RATE_LIMIT_BACKOFF_DEFAULT_SECONDS, delay)


def auto_invoke(
    surface,
    tool,
    payload,
    http_invoker=None,
    mcp_invoker=None,
    sleep_fn=time.sleep,
    rate_limit_scope=RATE_LIMIT_SCOPE_SHARED_OR_UNKNOWN,
):
    mcp_call = invoke if mcp_invoker is None else mcp_invoker
    if not _is_safe_auto_read(surface, tool):
        return mcp_call(surface, tool, payload)

    if rate_limit_scope not in {
        RATE_LIMIT_SCOPE_SHARED_OR_UNKNOWN,
        RATE_LIMIT_SCOPE_INDEPENDENT_PEER,
    }:
        raise ValueError(f"unsupported rate_limit_scope: {rate_limit_scope}")

    if http_invoker is None:
        from http_transport import invoke as http_call
    else:
        http_call = http_invoker

    primary = http_call(surface, tool, payload)
    attempts = [_attempt_record("http", surface, tool, primary)]
    if primary.get("status") == "OK":
        wrapped = _with_read_attempts(primary, attempts)
        wrapped["retry_policy"] = "peer-fallback-on-non-rate-limit"
        return wrapped

    if primary.get("status") == "RATE_LIMITED":
        backoff = _rate_limit_backoff_seconds(primary)
        if rate_limit_scope != RATE_LIMIT_SCOPE_INDEPENDENT_PEER:
            wrapped = _with_read_attempts(primary, attempts)
            wrapped["retry_policy"] = "rate-limit-backoff-no-peer"
            wrapped["observer_scope"] = rate_limit_scope
            wrapped["recommended_backoff_seconds"] = backoff
            wrapped["peer_fallback_skipped"] = "shared_or_unknown_edge_scope"
            return wrapped

        fallback = mcp_call(surface, tool, payload)
        attempts.append(_attempt_record("mcp", surface, tool, fallback))
        wrapped = _with_read_attempts(fallback, attempts)
        wrapped["retry_policy"] = "independent-peer-on-rate-limit"
        wrapped["observer_scope"] = rate_limit_scope
        wrapped["primary_recommended_backoff_seconds"] = backoff
        return wrapped

    fallback = mcp_call(surface, tool, payload)
    attempts.append(_attempt_record("mcp", surface, tool, fallback))
    wrapped = _with_read_attempts(fallback, attempts)
    wrapped["retry_policy"] = "peer-fallback-on-non-rate-limit"
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


def _blocked_identity_resolution(citizen_id, message, **extra):
    resolution = {
        "requested_citizen_id": citizen_id,
        "source": "live_citizens",
        "coverage_complete": False,
        **extra,
    }
    return {
        "status": "BLOCKED",
        "error": message,
        "identity_resolution": resolution,
    }


def _resolve_citizen_id(citizen_id, invoker, requested_transport=None):
    if isinstance(citizen_id, bool) or not isinstance(citizen_id, int) or citizen_id <= 0:
        return _blocked_identity_resolution(
            citizen_id,
            "citizen --id requires a positive integer citizen id",
            requested_transport=requested_transport,
        )

    since = None
    seen_since = set()
    matches = []
    pages_checked = 0
    rows_checked = 0
    expected_total = None
    page_provenance = []

    while True:
        payload = {} if since is None else {"since": since}
        page = invoker("read", "citizens", payload)
        pages_checked += 1
        page_provenance.append(
            {
                "since": since,
                "status": page.get("status"),
                "transport_policy": page.get("transport_policy"),
                "attempts": page.get("attempts"),
            }
        )

        if page.get("status") != "OK":
            result = dict(page)
            result["identity_resolution"] = {
                "requested_citizen_id": citizen_id,
                "source": "live_citizens",
                "requested_transport": requested_transport,
                "pages_checked": pages_checked,
                "rows_checked": rows_checked,
                "coverage_complete": False,
                "census_page_provenance": page_provenance,
            }
            return result

        data = page.get("data")
        if not isinstance(data, dict):
            return _blocked_identity_resolution(
                citizen_id,
                "live census page is not an object",
                requested_transport=requested_transport,
                pages_checked=pages_checked,
                rows_checked=rows_checked,
                census_page_provenance=page_provenance,
            )
        rows = data.get("citizens")
        if not isinstance(rows, list):
            return _blocked_identity_resolution(
                citizen_id,
                "live census page has no citizens list",
                requested_transport=requested_transport,
                pages_checked=pages_checked,
                rows_checked=rows_checked,
                census_page_provenance=page_provenance,
            )
        returned = data.get("returned")
        total = data.get("total")
        has_more = data.get("has_more")
        if isinstance(returned, bool) or not isinstance(returned, int) or returned != len(rows):
            return _blocked_identity_resolution(
                citizen_id,
                "live census returned count does not match page rows",
                requested_transport=requested_transport,
                pages_checked=pages_checked,
                rows_checked=rows_checked,
                census_page_provenance=page_provenance,
            )
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            return _blocked_identity_resolution(
                citizen_id,
                "live census total is not a non-negative integer",
                requested_transport=requested_transport,
                pages_checked=pages_checked,
                rows_checked=rows_checked,
                census_page_provenance=page_provenance,
            )
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            return _blocked_identity_resolution(
                citizen_id,
                "live census total changed during numeric-id resolution; retry required",
                requested_transport=requested_transport,
                pages_checked=pages_checked,
                rows_checked=rows_checked,
                census_page_provenance=page_provenance,
            )
        if not isinstance(has_more, bool):
            return _blocked_identity_resolution(
                citizen_id,
                "live census has_more is not boolean",
                requested_transport=requested_transport,
                pages_checked=pages_checked,
                rows_checked=rows_checked,
                census_page_provenance=page_provenance,
            )

        for row in rows:
            rows_checked += 1
            if not isinstance(row, dict):
                return _blocked_identity_resolution(
                    citizen_id,
                    "live census contains a non-object citizen row",
                    requested_transport=requested_transport,
                    pages_checked=pages_checked,
                    rows_checked=rows_checked,
                    census_page_provenance=page_provenance,
                )
            row_id = row.get("citizen_id")
            if row_id == citizen_id:
                handle = row.get("handle")
                if not isinstance(handle, str) or not handle:
                    return _blocked_identity_resolution(
                        citizen_id,
                        "matching census row has no valid handle",
                        requested_transport=requested_transport,
                        pages_checked=pages_checked,
                        rows_checked=rows_checked,
                        census_page_provenance=page_provenance,
                    )
                matches.append(handle)

        if not has_more:
            if rows_checked != expected_total:
                return _blocked_identity_resolution(
                    citizen_id,
                    "live census ended without proving complete coverage",
                    requested_transport=requested_transport,
                    pages_checked=pages_checked,
                    rows_checked=rows_checked,
                    expected_total=expected_total,
                    census_page_provenance=page_provenance,
                )
            break

        next_since = data.get("next_since")
        if (
            isinstance(next_since, bool)
            or not isinstance(next_since, int)
            or next_since < 0
            or next_since in seen_since
            or (since is not None and next_since <= since)
        ):
            return _blocked_identity_resolution(
                citizen_id,
                "live census pagination stalled or returned an invalid next_since",
                requested_transport=requested_transport,
                pages_checked=pages_checked,
                rows_checked=rows_checked,
                census_page_provenance=page_provenance,
            )
        seen_since.add(next_since)
        since = next_since

    if len(matches) != 1:
        return _blocked_identity_resolution(
            citizen_id,
            f"live complete census must contain exactly one citizen_id {citizen_id}; found {len(matches)}",
            requested_transport=requested_transport,
            pages_checked=pages_checked,
            rows_checked=rows_checked,
            expected_total=expected_total,
            coverage_complete=True,
            census_page_provenance=page_provenance,
        )

    return {
        "status": "RESOLVED",
        "resolved_handle": matches[0],
        "identity_resolution": {
            "requested_citizen_id": citizen_id,
            "resolved_handle": matches[0],
            "source": "live_citizens",
            "requested_transport": requested_transport,
            "pages_checked": pages_checked,
            "rows_checked": rows_checked,
            "coverage_complete": True,
            "census_page_provenance": page_provenance,
        },
    }


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


def _reconciliation_fingerprint(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _comparison_evidence(readback_id, expected, observed):
    keys = sorted(set(expected) | set(observed))
    mismatched = [key for key in keys if expected.get(key) != observed.get(key)]
    return {
        "readback_id": readback_id,
        "mismatched_fields": mismatched,
        "expected_sha256": _reconciliation_fingerprint(expected),
        "observed_sha256": _reconciliation_fingerprint(observed),
    }


def _reconciliation_unknown(
    operations_path,
    record,
    reason,
    status="OK",
    evidence=None,
    error=None,
):
    try:
        forum_ledger.record_reconciliation(
            operations_path,
            record["id"],
            "unknown",
            evidence=evidence,
            error=error or reason,
        )
    except (forum_ledger.LedgerError, OSError) as exc:
        return {
            "status": "BLOCKED",
            "operation_id": record["id"],
            "operation": record["operation"],
            "delivery_state": "unknown",
            "reason": reason,
            "ledger_error": str(exc),
        }
    return {
        "status": status,
        "operation_id": record["id"],
        "operation": record["operation"],
        "delivery_state": "unknown",
        "reason": reason,
    }


def _reconciliation_contradiction(operations_path, record, evidence, reason):
    try:
        forum_ledger.record_reconciliation(
            operations_path,
            record["id"],
            "contradiction",
            evidence=evidence,
            error=reason,
        )
    except (forum_ledger.LedgerError, OSError) as exc:
        return {
            "status": "BLOCKED",
            "operation_id": record["id"],
            "operation": record["operation"],
            "delivery_state": "contradiction",
            "reason": reason,
            "ledger_error": str(exc),
        }
    return {
        "status": "OK",
        "operation_id": record["id"],
        "operation": record["operation"],
        "delivery_state": "contradiction",
        "reason": reason,
    }


def _reconciliation_match(operations_path, record, evidence):
    try:
        forum_ledger.mark_reconciled_verified(
            operations_path,
            record["id"],
            evidence=evidence,
        )
    except (forum_ledger.LedgerError, OSError) as exc:
        return {
            "status": "BLOCKED",
            "operation_id": record["id"],
            "operation": record["operation"],
            "delivery_state": "recovered_match",
            "reason": "remote match is proven but reconciliation receipt could not persist",
            "ledger_error": str(exc),
        }
    result = {
        "status": "OK",
        "operation_id": record["id"],
        "operation": record["operation"],
        "delivery_state": "recovered_match",
    }
    if isinstance(evidence.get("readback_id"), int):
        result["readback_id"] = evidence["readback_id"]
    return result


def _reconcile_operation(operation_id, invoker, state_path, operations_path):
    if operations_path is None:
        return {"status": "BLOCKED", "error": "operation ledger path is not configured"}
    try:
        record = forum_ledger.get_operation(operations_path, operation_id)
    except forum_ledger.LedgerError as exc:
        return {"status": "BLOCKED", "error": str(exc)}

    if record["state"] in {"VERIFIED", "BLOCKED"}:
        return {
            "status": "OK",
            "operation_id": record["id"],
            "operation": record["operation"],
            "delivery_state": "already_terminal",
            "operation_state": record["state"],
        }

    operation = record["operation"]
    intent = record["intent"]
    evidence = record["evidence"]

    if operation == "vote":
        return _reconciliation_unknown(
            operations_path,
            record,
            "vote identity cannot be proven from public aggregate count",
            evidence={"reconciliation_scope": "no_identity_safe_vote_readback"},
        )

    if operation in {"post", "comment"}:
        write_id = evidence.get("write_id")
        if not isinstance(write_id, int):
            return _reconciliation_unknown(
                operations_path,
                record,
                "no exact write id is available; partial history is not evidence of absence",
                evidence={"reconciliation_scope": "no_exact_write_id"},
            )
        tool = "read_post" if operation == "post" else "read_comment"
        readback = invoker("read", tool, {f"{operation}_id": write_id})
        if readback.get("status") != "OK":
            reason = f"exact {operation} readback is unavailable"
            result = _reconciliation_unknown(
                operations_path,
                record,
                reason,
                status=readback.get("status", "BLOCKED"),
                evidence={
                    "readback_id": write_id,
                    "readback_status": readback.get("status"),
                },
                error=readback.get("error") or reason,
            )
            if isinstance(readback.get("route"), str):
                result["route"] = readback["route"]
            return result

        remote = _record_from_readback(readback, operation)
        if not isinstance(remote, dict) or remote.get("id") != write_id:
            return _reconciliation_unknown(
                operations_path,
                record,
                f"exact {operation} endpoint did not return the expected object",
                evidence={"readback_id": write_id, "readback_status": "OK"},
            )

        if operation == "post":
            expected = {
                "id": write_id,
                "title": intent.get("title"),
                "body": intent.get("body"),
            }
            observed = {
                "id": remote.get("id"),
                "title": remote.get("title"),
                "body": remote.get("body"),
            }
            if "url" in intent:
                expected["url"] = intent.get("url")
                observed["url"] = remote.get("url")
        else:
            expected = {
                "id": write_id,
                "post_id": intent.get("post_id"),
                "parent_id": intent.get("parent_id"),
                "body": intent.get("body"),
            }
            observed = {
                "id": remote.get("id"),
                "post_id": remote.get("post_id"),
                "parent_id": remote.get("parent_id"),
                "body": remote.get("body"),
            }

        comparison = _comparison_evidence(write_id, expected, observed)
        if observed != expected:
            return _reconciliation_contradiction(
                operations_path,
                record,
                comparison,
                f"exact {operation} object contradicts durable intent",
            )
        return _reconciliation_match(operations_path, record, comparison)

    if operation == "ack":
        cursor = intent.get("up_to")
        if not isinstance(cursor, dict):
            return _reconciliation_unknown(
                operations_path,
                record,
                "ack durable intent has no exact cursor",
                evidence={"reconciliation_scope": "missing_ack_cursor"},
            )
        readback = invoker("citizen", "me", {"cursor_mode": "id"})
        if readback.get("status") != "OK" or not _verify_ack(cursor, readback):
            return _reconciliation_unknown(
                operations_path,
                record,
                "current inbox state does not prove acknowledgement progress",
                status=readback.get("status", "OK"),
                evidence={"readback_status": readback.get("status")},
                error=readback.get("error"),
            )
        try:
            if pathlib.Path(state_path).exists():
                forum_state.commit_verified_ack(state_path, cursor)
        except (forum_state.StateError, OSError) as exc:
            try:
                forum_ledger.record_reconciliation(
                    operations_path,
                    record["id"],
                    "recovered_match",
                    evidence={"remote_verified": True},
                    error=f"remote ack is proven but local inbox state could not advance: {exc}",
                )
            except (forum_ledger.LedgerError, OSError):
                pass
            return {
                "status": "BLOCKED",
                "operation_id": record["id"],
                "operation": "ack",
                "delivery_state": "recovered_match",
                "reason": f"remote ack is proven but local inbox state could not advance: {exc}",
            }
        return _reconciliation_match(
            operations_path,
            record,
            {"remote_verified": True},
        )

    return _reconciliation_unknown(
        operations_path,
        record,
        f"operation type has no reconciliation contract: {operation}",
        evidence={"reconciliation_scope": "unsupported_operation"},
    )


def execute(
    args,
    invoker=invoke,
    state_path=STATE,
    operations_path=None,
    liveness_path=None,
    liveness_interval_s=None,
    stale_after_intervals=forum_liveness.DEFAULT_STALE_AFTER_INTERVALS,
):
    if args.command == "reconcile":
        return _reconcile_operation(
            args.operation_id,
            invoker=invoker,
            state_path=state_path,
            operations_path=operations_path,
        )

    if args.command == "operations":
        if operations_path is None:
            return {"status": "BLOCKED", "error": "operation ledger path is not configured"}
        try:
            return {"status": "OK", "data": forum_ledger.operations_summary(operations_path)}
        except forum_ledger.LedgerError as exc:
            return {"status": "BLOCKED", "error": f"could not read operation ledger: {exc}"}

    if args.command == "state":
        try:
            data = forum_state.state_summary(state_path)
            if liveness_path is not None:
                data["cursor_liveness"] = forum_liveness.summary(liveness_path)
            return {"status": "OK", "data": data}
        except (forum_state.StateError, forum_liveness.LivenessError) as exc:
            return {"status": "BLOCKED", "error": f"could not read durable state: {exc}"}

    if args.command == "citizen" and getattr(args, "citizen_id", None) is not None:
        if getattr(args, "handle", None):
            return {
                "status": "BLOCKED",
                "error": "citizen accepts either HANDLE or --id CITIZEN_ID, not both",
            }
        resolved = _resolve_citizen_id(
            args.citizen_id,
            invoker,
            requested_transport=getattr(args, "transport", None),
        )
        if resolved.get("status") != "RESOLVED":
            return resolved
        exact = invoker("read", "citizen", {"handle": resolved["resolved_handle"]})
        result = dict(exact)
        result["identity_resolution"] = resolved["identity_resolution"]
        return result

    if args.command == "thread":
        return _read_complete_thread(args.post_id, invoker)

    if args.command == "inbox":
        try:
            summary = forum_state.state_summary(state_path)
        except (forum_state.StateError, OSError, ValueError) as exc:
            return {"status": "BLOCKED", "error": f"could not read durable state: {exc}"}
        if summary.get("state") == "PENDING":
            try:
                replay = forum_state.replay_pending_inbox(state_path)
            except forum_state.StateError as exc:
                return {
                    "status": "BLOCKED",
                    "error": f"could not replay durably banked inbox work: {exc}",
                    "data": summary,
                }
            return {"status": "OK", "durable_replay": True, "data": replay}

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
        if written.get("status") == "RATE_LIMITED":
            backoff = _rate_limit_backoff_seconds(written)
            evidence = {
                **_write_evidence(written),
                "delivery_state": "not_executed",
                "recommended_backoff_seconds": backoff,
            }
            ledger_error = _ledger_transition(
                operations_path,
                operation_id,
                "BLOCKED",
                evidence=evidence,
                error=written.get("error") or "edge rate limit rejected ack before registry execution",
            )
            result = {
                "status": "RATE_LIMITED",
                "operation": "ack",
                "delivery_state": "not_executed",
                "recommended_backoff_seconds": backoff,
                "error": written.get("error") or "edge rate limit rejected ack before registry execution",
            }
            if operation_id is not None:
                result["operation_id"] = operation_id
            if isinstance(written.get("route"), str):
                result["route"] = written["route"]
            if ledger_error is not None:
                result["ledger_error"] = str(ledger_error)
            return result
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
        if liveness_path is not None:
            try:
                liveness = forum_liveness.record_verified_ack(
                    liveness_path,
                    cursor_mode="id",
                )
                result["cursor_liveness"] = {
                    "read_freshness": liveness["read_freshness"],
                    "last_verified_ack_at_ms": liveness["last_verified_ack_at_ms"],
                }
            except (forum_liveness.LivenessError, OSError, ValueError) as exc:
                result["cursor_liveness"] = {
                    "read_freshness": "UNKNOWN",
                    "error": f"could not persist verified ack liveness: {exc}",
                }
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
        if args.command in {"post", "comment"} and result.get("status") == "RATE_LIMITED":
            backoff = _rate_limit_backoff_seconds(result)
            evidence = {
                **_write_evidence(result),
                "delivery_state": "not_executed",
                "recommended_backoff_seconds": backoff,
            }
            ledger_error = _ledger_transition(
                operations_path,
                operation_id,
                "BLOCKED",
                evidence=evidence,
                error=result.get("error") or "edge rate limit rejected write before registry execution",
            )
            payload_out = {
                "status": "RATE_LIMITED",
                "operation": args.command,
                "delivery_state": "not_executed",
                "recommended_backoff_seconds": backoff,
                "error": result.get("error") or "edge rate limit rejected write before registry execution",
            }
            if operation_id is not None:
                payload_out["operation_id"] = operation_id
            if isinstance(result.get("route"), str):
                payload_out["route"] = result["route"]
            if ledger_error is not None:
                payload_out["ledger_error"] = str(ledger_error)
            return payload_out
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

    if args.command == "search":
        return _with_search_completeness(result)

    if args.command == "watch":
        data = result.get("data") or {}
        if liveness_path is None:
            return result
        try:
            liveness = forum_liveness.observe_pulse(
                liveness_path,
                data,
                configured_interval_s=liveness_interval_s,
                stale_after_intervals=stale_after_intervals,
            )
            result["read_freshness"] = liveness["read_freshness"]
            result["wake_signal_usable"] = liveness["wake_signal_usable"]
            result["cursor_liveness"] = liveness
        except (forum_liveness.LivenessError, OSError, ValueError) as exc:
            result["read_freshness"] = "UNKNOWN"
            result["wake_signal_usable"] = False
            result["cursor_liveness"] = {
                "read_freshness": "UNKNOWN",
                "error": f"could not classify cursor liveness: {exc}",
            }
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
    configured_interval = os.environ.get("JESTER_FORUM_POLL_INTERVAL_S")
    stale_after = os.environ.get(
        "JESTER_FORUM_STALE_AFTER_INTERVALS",
        str(forum_liveness.DEFAULT_STALE_AFTER_INTERVALS),
    )
    result = execute(
        args,
        invoker=transport_invoker(args.transport),
        operations_path=OPERATIONS,
        liveness_path=LIVENESS,
        liveness_interval_s=configured_interval,
        stale_after_intervals=stale_after,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"OK", "WRITE_VERIFIED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
