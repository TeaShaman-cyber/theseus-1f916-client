#!/usr/bin/env python3
import urllib.error
import pathlib
import urllib.parse

import forum_cache
from client import request_with_meta


ROOT = pathlib.Path(__file__).resolve().parent
CACHE = ROOT / ".forum-cache.json"


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
        if tool == "citizens":
            return "GET", _query("/api/citizens", {"since": payload.get("since")}), None, False
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


def _header(headers, name):
    if headers is None:
        return None
    try:
        value = headers.get(name)
    except AttributeError:
        return None
    return value if isinstance(value, str) and value else None


def _response_parts(value):
    if hasattr(value, "data") and hasattr(value, "headers"):
        return value.data, int(getattr(value, "status", 200)), value.headers
    return value, 200, None


def _public_cacheable(surface, method):
    return surface == "read" and method == "GET"


def _cache_control_no_store(headers):
    value = _header(headers, "Cache-Control")
    return bool(value and "no-store" in value.lower())



def _etag_opaque_tag(value):
    if not isinstance(value, str):
        return None
    raw = value[2:] if value.startswith("W/") else value
    if len(raw) < 2 or raw[0] != '"' or raw[-1] != '"':
        return None
    opaque = raw[1:-1]
    for char in opaque:
        code = ord(char)
        if char == '"' or code < 0x21 or code == 0x7F:
            return None
    return opaque


def _weak_etag_equal(left, right):
    left_tag = _etag_opaque_tag(left)
    right_tag = _etag_opaque_tag(right)
    return left_tag is not None and right_tag is not None and left_tag == right_tag


def _caller_revalidation(route, sent_etag, response_etag=None):
    if response_etag is not None and not _weak_etag_equal(response_etag, sent_etag):
        return {
            "status": "BLOCKED",
            "route": route,
            "revalidation_status": "VALIDATOR_MISMATCH",
            "error": "HTTP 304 validator does not match the caller validator",
        }
    return {
        "status": "NOT_MODIFIED",
        "route": route,
        "revalidation_status": "NOT_MODIFIED",
        "etag": sent_etag,
    }

def _cached_revalidation(route, entry, sent_etag, response_etag=None):
    if entry is None or not sent_etag or entry.get("etag") != sent_etag:
        return {
            "status": "BLOCKED",
            "route": route,
            "cache_status": "MISS_ON_304",
            "error": "HTTP 304 arrived without a matching cached validator/body",
        }
    if response_etag is not None and not _weak_etag_equal(response_etag, sent_etag):
        return {
            "status": "BLOCKED",
            "route": route,
            "cache_status": "VALIDATOR_MISMATCH",
            "error": "HTTP 304 validator does not match the cached validator",
        }
    return {
        "status": "OK",
        "route": route,
        "data": entry["data"],
        "cache_status": "REVALIDATED",
        "etag": sent_etag,
    }

def _normalize(tool, data):
    if tool != "search" or not isinstance(data, dict):
        return data
    results = data.get("results")
    if not isinstance(results, list):
        return data
    normalized = {
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
    for key in ("has_more", "count", "returned", "limit", "max_limit", "note"):
        if key in data:
            normalized[key] = data[key]
    return normalized


def invoke(surface, tool, payload, requester=None, cache_path=CACHE, if_none_match=None):
    if requester is None:
        requester = request_with_meta
    try:
        method, path, body, auth = call_spec(surface, tool, payload)
    except (KeyError, TypeError, ValueError) as exc:
        return {
            "status": "BLOCKED",
            "route": f"http:{surface}.{tool}",
            "error": str(exc),
        }

    route = f"http:{method} {path.split('?', 1)[0]}"
    if if_none_match is not None:
        if _etag_opaque_tag(if_none_match) is None:
            return {
                "status": "BLOCKED",
                "route": route,
                "error": "if_none_match must be one valid quoted ETag",
            }
        if not _public_cacheable(surface, method):
            return {
                "status": "BLOCKED",
                "route": route,
                "error": "explicit conditional validator is supported only for public GET reads",
            }

    cache_enabled = cache_path is not None and _public_cacheable(surface, method)
    cache_key = forum_cache.cache_key(method, path) if cache_enabled else None
    cache_entry = forum_cache.lookup(cache_path, cache_key) if cache_enabled else None
    cached_etag = cache_entry.get("etag") if isinstance(cache_entry, dict) else None
    sent_etag = if_none_match if if_none_match is not None else cached_etag
    conditional_headers = {"If-None-Match": sent_etag} if sent_etag else None

    try:
        if conditional_headers is None:
            response = requester(path, method=method, payload=body, auth=auth)
        else:
            response = requester(
                path,
                method=method,
                payload=body,
                auth=auth,
                headers=conditional_headers,
            )
        data, response_status, response_headers = _response_parts(response)
        if response_status == 304:
            if if_none_match is not None:
                return _caller_revalidation(
                    route,
                    sent_etag,
                    response_etag=_header(response_headers, "ETag"),
                )
            return _cached_revalidation(
                route,
                cache_entry,
                sent_etag,
                response_etag=_header(response_headers, "ETag"),
            )
        if response_status < 200 or response_status >= 300:
            return {
                "status": "BLOCKED",
                "route": route,
                "error": f"unexpected HTTP status {response_status}",
            }
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            if if_none_match is not None:
                return _caller_revalidation(
                    route,
                    sent_etag,
                    response_etag=_header(exc.headers, "ETag"),
                )
            return _cached_revalidation(
                route,
                cache_entry,
                sent_etag,
                response_etag=_header(exc.headers, "ETag"),
            )
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

    normalized = _normalize(tool, data)
    result = {"status": "OK", "data": normalized}
    response_etag = _header(response_headers, "ETag")
    if response_etag is not None:
        result["etag"] = response_etag
    if if_none_match is not None:
        result["revalidation_status"] = "FULL_RESPONSE"
    if not cache_enabled or response_headers is None:
        return result

    if _cache_control_no_store(response_headers):
        try:
            forum_cache.invalidate(cache_path, cache_key)
        except (OSError, ValueError, TypeError) as exc:
            result["cache_status"] = "DEGRADED"
            result["cache_error"] = str(exc)[:1000]
            return result
        result["cache_status"] = "NO_STORE"
        return result

    if not response_etag:
        try:
            forum_cache.invalidate(cache_path, cache_key)
        except (OSError, ValueError, TypeError) as exc:
            result["cache_status"] = "DEGRADED"
            result["cache_error"] = str(exc)[:1000]
            return result
        result["cache_status"] = "NO_VALIDATOR"
        return result

    try:
        forum_cache.store(
            cache_path,
            cache_key,
            route=route,
            etag=response_etag,
            data=normalized,
        )
    except (OSError, ValueError, TypeError) as exc:
        result["cache_status"] = "DEGRADED"
        result["cache_error"] = str(exc)[:1000]
        result["etag"] = response_etag
        return result
    result["cache_status"] = "STORED"
    result["etag"] = response_etag
    return result
