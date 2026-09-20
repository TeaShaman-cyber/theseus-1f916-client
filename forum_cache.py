import copy
import hashlib
import json
import os
import pathlib
import tempfile
import time


CACHE_SCHEMA_VERSION = 1


def _now_ms():
    return time.time_ns() // 1_000_000


def _empty(now_ms=None):
    now = _now_ms() if now_ms is None else int(now_ms)
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "created_at_ms": now,
        "updated_at_ms": now,
        "entries": {},
    }



def _data_sha256(data):
    payload = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()

def cache_key(method, path):
    identity = json.dumps(
        {"method": str(method).upper(), "path": str(path)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


def _validate(raw):
    if not isinstance(raw, dict):
        raise ValueError("cache must be a JSON object")
    if raw.get("schema_version") != CACHE_SCHEMA_VERSION:
        raise ValueError("unsupported cache schema")
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        raise ValueError("cache entries must be an object")
    for key, entry in entries.items():
        if not isinstance(key, str) or len(key) != 64:
            raise ValueError("cache entry key must be sha256 text")
        if not isinstance(entry, dict):
            raise ValueError("cache entry must be an object")
        if not isinstance(entry.get("etag"), str) or not entry["etag"]:
            raise ValueError("cache entry etag must be non-empty")
        if not isinstance(entry.get("route"), str) or not entry["route"]:
            raise ValueError("cache entry route must be non-empty")
        if "data" not in entry:
            raise ValueError("cache entry data missing")
        expected_hash = entry.get("data_sha256")
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            raise ValueError("cache entry data_sha256 missing")
        if _data_sha256(entry["data"]) != expected_hash:
            raise ValueError("cache entry data hash mismatch")
        try:
            int(entry["stored_at_ms"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("cache entry stored_at_ms must be integer") from exc
    return raw


def load_cache(path):
    path = pathlib.Path(path)
    if not path.exists():
        return _empty()
    try:
        raw = json.loads(path.read_text())
        return _validate(raw)
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return _empty()


def lookup(path, key):
    entry = load_cache(path)["entries"].get(key)
    return copy.deepcopy(entry) if entry is not None else None


def _atomic_write(path, raw):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.tmp-",
            delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if temp_name is not None and os.path.exists(temp_name):
            os.unlink(temp_name)


def store(path, key, route, etag, data, now_ms=None):
    if not isinstance(etag, str) or not etag:
        raise ValueError("etag must be non-empty")
    now = _now_ms() if now_ms is None else int(now_ms)
    raw = load_cache(path)
    if not raw["entries"]:
        raw["created_at_ms"] = now
    raw["entries"][key] = {
        "route": str(route),
        "etag": etag,
        "data": copy.deepcopy(data),
        "data_sha256": _data_sha256(data),
        "stored_at_ms": now,
    }
    raw["updated_at_ms"] = now
    _validate(raw)
    _atomic_write(path, raw)
    return copy.deepcopy(raw["entries"][key])


def invalidate(path, key, now_ms=None):
    path = pathlib.Path(path)
    if not path.exists():
        return False
    raw = load_cache(path)
    if key not in raw["entries"]:
        # Corrupt/unsupported cache is disposable. Remove it instead of preserving
        # unusable validator state.
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return False
    del raw["entries"][key]
    if not raw["entries"]:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return True
    raw["updated_at_ms"] = _now_ms() if now_ms is None else int(now_ms)
    _atomic_write(path, raw)
    return True
