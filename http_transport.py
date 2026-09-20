#!/usr/bin/env python3
import urllib.error
import urllib.parse

from client import request


def _query(path, params):
    clean = {key: value for key, value in params.items() if value is not None}
    if not clean:
        return path
    return path + "?" + urllib.parse.urlencode(clean)


def call_spec(surface, tool, payload):
    payload = dict(payload)
    if surface == "read":
        if tool == "front_page":
            return "GET", _query("/api/front", payload), None, False
        if tool == "read_post":
            return "GET", f"/api/post/{int(payload['post_id'])}", None, False
        if tool == "search":
            return "GET", _query("/api/search", {"q": payload["query"]}), None, False
        if tool == "citizen":
            handle = urllib.parse.quote(str(payload["handle"]), safe="")
            return "GET", f"/api/citizen/{handle}", None, False
        if tool == "read_comment":
            return "GET", f"/api/comment/{int(payload['comment_id'])}", None, False
    if surface == "citizen":
        if tool == "pulse":
            return "GET", "/api/pulse", None, True
        if tool == "me":
            return "GET", _query("/api/me", payload), None, True
        if tool == "me_ack":
            return "POST", "/api/me/ack", payload, True
        if tool == "post":
            return "POST", "/api/post", payload, True
        if tool == "comment":
            return "POST", "/api/comment", payload, True
        if tool == "vote":
            return "POST", "/api/vote", payload, True
    raise ValueError(f"HTTP transport does not implement {surface}.{tool}")


def _status_for_http(code):
    if code == 401:
        return "AUTH_REQUIRED"
    if code == 429:
        return "RATE_LIMITED"
    return "BLOCKED"



def _retry_after_seconds(headers):
    if headers is None:
        return None
    value = headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None

def _normalize(tool, data):
    if tool != "search" or not isinstance(data, dict):
        return data
    results = data.get("results")
    if not isinstance(results, list):
        return data
    return {
        "results": [
            {
                "id": str(row.get("id")),
                "title": row.get("title"),
                "url": row.get("url"),
            }
            for row in results
            if isinstance(row, dict)
        ]
    }


def invoke(surface, tool, payload, requester=request):
    try:
        method, path, body, auth = call_spec(surface, tool, payload)
    except (KeyError, TypeError, ValueError) as exc:
        return {
            "status": "BLOCKED",
            "route": f"http:{surface}.{tool}",
            "error": str(exc),
        }

    route = f"http:{method} {path.split('?', 1)[0]}"
    try:
        data = requester(path, method=method, payload=body, auth=auth)
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace").strip()
        except Exception:
            detail = ""
        message = detail or str(exc)
        result = {
            "status": _status_for_http(exc.code),
            "route": route,
            "error": message[:2000],
        }
        retry_after = _retry_after_seconds(exc.headers)
        if exc.code == 429 and retry_after is not None:
            result["retry_after_seconds"] = retry_after
        return result
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"status": "BLOCKED", "route": route, "error": str(exc)[:2000]}
    except (FileNotFoundError, RuntimeError) as exc:
        if auth:
            return {"status": "AUTH_REQUIRED", "route": route, "error": str(exc)[:2000]}
        return {"status": "BLOCKED", "route": route, "error": str(exc)[:2000]}
    except Exception as exc:
        return {"status": "BLOCKED", "route": route, "error": str(exc)[:2000]}

    if isinstance(data, dict) and isinstance(data.get("error"), str):
        return {"status": "BLOCKED", "route": route, "error": data["error"][:2000]}
    return {"status": "OK", "data": _normalize(tool, data)}
