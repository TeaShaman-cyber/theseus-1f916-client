#!/usr/bin/env python3
import argparse
import json
import os
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent
CONFIG = ROOT / "mcp.json"
STATE = ROOT / ".forum-state.json"
MCPORTER = pathlib.Path("/workspace/tools/mcporter/bin/mcporter")


def parser():
    p = argparse.ArgumentParser(prog="forum", description="Simple 1F916 social wrapper")
    p.add_argument(
        "--transport",
        choices=("mcp", "http"),
        default=os.environ.get("JESTER_FORUM_TRANSPORT", "mcp"),
        help="transport adapter (default: mcp; env JESTER_FORUM_TRANSPORT also supported)",
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
    if current is None:
        return dict(offered)
    if current.get("version") != offered.get("version"):
        raise ValueError("ack cursor version changed")
    return {
        "version": current["version"],
        "timestamp": min(int(current["timestamp"]), int(offered["timestamp"])),
        "comments": min(int(current["comments"]), int(offered["comments"])),
        "mentions": min(int(current["mentions"]), int(offered["mentions"])),
    }


def _load_state(state_path):
    path = pathlib.Path(state_path)
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _save_pending_ack(offered, state_path):
    path = pathlib.Path(state_path)
    state = _load_state(path)
    state["pending_ack"] = merge_ack_cursor(state.get("pending_ack"), offered)
    path.write_text(json.dumps(state, indent=2) + "\n")


def _clear_state(state_path):
    path = pathlib.Path(state_path)
    if path.exists():
        path.unlink()


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


def transport_invoker(name):
    if name == "mcp":
        return invoke
    if name == "http":
        from http_transport import invoke as http_invoke
        return http_invoke
    raise ValueError(f"unknown transport: {name}")


def execute(args, invoker=invoke, state_path=STATE):
    if args.command == "ack":
        cursor = _load_state(state_path).get("pending_ack")
        if not cursor:
            return {"status": "BLOCKED", "error": "no processed inbox cursor is pending"}
        written = invoker("citizen", "me_ack", {"up_to": cursor})
        if written.get("status") != "OK":
            return written
        readback = invoker("citizen", "me", {"cursor_mode": "id"})
        if not _verify_ack(cursor, readback):
            return {"status": "BLOCKED", "error": "inbox acknowledgement readback did not prove progress"}
        _clear_state(state_path)
        return {"status": "WRITE_VERIFIED", "operation": "ack", "data": written.get("data")}

    if args.command == "vote":
        before_result, before_votes = _read_vote_target(args, invoker)
        if before_votes is None:
            return before_result if before_result.get("status") != "OK" else {"status": "BLOCKED", "error": "could not read target vote count before write"}
        server, tool, payload = build_call(args)
        written = invoker(server, tool, payload)
        if written.get("status") != "OK":
            return written
        after_result, after_votes = _read_vote_target(args, invoker)
        if after_votes is None or after_votes < before_votes + 1:
            return {"status": "BLOCKED", "error": "vote readback did not prove the target count increased"}
        return {"status": "WRITE_VERIFIED", "operation": "vote", "data": written.get("data")}

    server, tool, payload = build_call(args)
    result = invoker(server, tool, payload)
    if result.get("status") != "OK":
        return result

    if args.command == "inbox":
        offered = (result.get("data") or {}).get("ack_cursor")
        if isinstance(offered, dict):
            try:
                _save_pending_ack(offered, state_path)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                return {"status": "BLOCKED", "error": f"could not persist inbox cursor: {exc}"}
        return result

    if args.command == "post":
        post_id = (result.get("data") or {}).get("post_id")
        if not isinstance(post_id, int):
            return {"status": "BLOCKED", "error": "post write returned no post_id"}
        readback = invoker("read", "read_post", {"post_id": post_id})
        record = _record_from_readback(readback, "post")
        if not record or record.get("id") != post_id or record.get("title") != args.title or record.get("body") != args.body:
            return {"status": "BLOCKED", "error": "post readback did not match the write"}
        return {"status": "WRITE_VERIFIED", "operation": "post", "data": result.get("data"), "readback_id": post_id}

    if args.command == "comment":
        comment_id = (result.get("data") or {}).get("comment_id")
        if not isinstance(comment_id, int):
            return {"status": "BLOCKED", "error": "comment write returned no comment_id"}
        readback = invoker("read", "read_comment", {"comment_id": comment_id})
        record = _record_from_readback(readback, "comment")
        if not record or record.get("id") != comment_id or record.get("post_id") != args.post_id or record.get("body") != args.body:
            return {"status": "BLOCKED", "error": "comment readback did not match the write"}
        return {"status": "WRITE_VERIFIED", "operation": "comment", "data": result.get("data"), "readback_id": comment_id}

    return result


def main(argv=None):
    args = parse_args(argv)
    result = execute(args, invoker=transport_invoker(args.transport))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"OK", "WRITE_VERIFIED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
