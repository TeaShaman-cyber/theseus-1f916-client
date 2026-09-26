import copy
import json
import os
import pathlib
import tempfile
import time


STATE_SCHEMA_VERSION = 1
CURSOR_ORDER_FIELDS = ("timestamp", "comments", "mentions")


class StateError(ValueError):
    pass


def _now_ms():
    return time.time_ns() // 1_000_000


def _validate_cursor(cursor, label="ack cursor"):
    if not isinstance(cursor, dict):
        raise StateError(f"{label} must be an object")
    if cursor.get("version") is None:
        raise StateError(f"{label} has no version")
    for field in CURSOR_ORDER_FIELDS:
        try:
            int(cursor[field])
        except (KeyError, TypeError, ValueError) as exc:
            raise StateError(f"{label}.{field} must be an integer") from exc
    return cursor


def ack_cursor_leq(left, right):
    _validate_cursor(left, "left ack cursor")
    _validate_cursor(right, "right ack cursor")
    return all(int(left[field]) <= int(right[field]) for field in CURSOR_ORDER_FIELDS)


def merge_ack_cursor(current, offered):
    _validate_cursor(offered, "offered ack cursor")
    if current is None:
        return copy.deepcopy(offered)
    _validate_cursor(current, "current ack cursor")
    if current.get("version") != offered.get("version"):
        raise StateError("ack cursor version changed")
    if ack_cursor_leq(current, offered):
        return copy.deepcopy(current)
    if ack_cursor_leq(offered, current):
        return copy.deepcopy(offered)
    raise StateError("ack cursors are not safely ordered; refusing to synthesize cursor")


def _empty_state(now_ms):
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "created_at_ms": int(now_ms),
        "updated_at_ms": int(now_ms),
        "pending_ack": None,
        "banked_reads": [],
        "recovery": None,
    }


def _legacy_state(raw, path):
    if not isinstance(raw, dict):
        raise StateError("legacy state must be an object")
    pending = raw.get("pending_ack")
    if pending is None and not raw:
        observed = int(path.stat().st_mtime * 1000)
        return _empty_state(observed)
    if not isinstance(pending, dict):
        raise StateError("unversioned state is not a recognized legacy pending-ack file")
    _validate_cursor(pending, "legacy pending ack")
    observed = int(path.stat().st_mtime * 1000)
    state = _empty_state(observed)
    state["recovery"] = {
        "kind": "legacy_pending_ack",
        "cursor": copy.deepcopy(pending),
    }
    return state


def _validate_state(raw):
    if not isinstance(raw, dict):
        raise StateError("state must be a JSON object")
    version = raw.get("schema_version")
    if version != STATE_SCHEMA_VERSION:
        raise StateError(f"unsupported state schema: {version!r}")
    required = (
        "created_at_ms",
        "updated_at_ms",
        "pending_ack",
        "banked_reads",
        "recovery",
    )
    missing = [key for key in required if key not in raw]
    if missing:
        raise StateError(f"state schema v1 missing fields: {', '.join(missing)}")
    try:
        int(raw["created_at_ms"])
        int(raw["updated_at_ms"])
    except (TypeError, ValueError) as exc:
        raise StateError("state timestamps must be integers") from exc
    if not isinstance(raw["banked_reads"], list):
        raise StateError("banked_reads must be a list")
    if raw["pending_ack"] is not None:
        _validate_cursor(raw["pending_ack"], "pending_ack")
    recovery = raw["recovery"]
    if recovery is not None:
        if not isinstance(recovery, dict) or recovery.get("kind") != "legacy_pending_ack":
            raise StateError("unsupported recovery state")
        _validate_cursor(recovery.get("cursor"), "recovery cursor")
    for index, banked in enumerate(raw["banked_reads"]):
        if not isinstance(banked, dict):
            raise StateError(f"banked_reads[{index}] must be an object")
        try:
            int(banked["banked_at_ms"])
        except (KeyError, TypeError, ValueError) as exc:
            raise StateError(f"banked_reads[{index}].banked_at_ms must be an integer") from exc
        _validate_cursor(banked.get("ack_cursor"), f"banked_reads[{index}].ack_cursor")
        if not isinstance(banked.get("since_last_visit"), dict):
            raise StateError(f"banked_reads[{index}].since_last_visit must be an object")

    if recovery is not None:
        if raw["pending_ack"] is not None or raw["banked_reads"]:
            raise StateError("recovery state cannot also contain ackable banked work")
    elif raw["banked_reads"]:
        if raw["pending_ack"] is None:
            raise StateError("banked work requires an exact pending_ack floor")
        floor = None
        for banked in raw["banked_reads"]:
            floor = merge_ack_cursor(floor, banked["ack_cursor"])
        if raw["pending_ack"] != floor:
            raise StateError("pending_ack does not match the exact banked cursor floor")
    elif raw["pending_ack"] is not None:
        raise StateError("pending_ack without banked work is not ackable")
    return raw


def load_state(state_path):
    path = pathlib.Path(state_path)
    if not path.exists():
        return _empty_state(_now_ms())
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"could not read durable state: {exc}") from exc
    if isinstance(raw, dict) and "schema_version" not in raw:
        return _legacy_state(raw, path)
    return _validate_state(raw)


def _atomic_write(path, state):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, indent=2, ensure_ascii=False) + "\n"
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


def bank_inbox_page(state_path, data, now_ms=None):
    if not isinstance(data, dict):
        raise StateError("inbox data must be an object")
    offered = data.get("ack_cursor")
    _validate_cursor(offered, "inbox ack_cursor")
    work = data.get("since_last_visit")
    if not isinstance(work, dict):
        raise StateError("inbox since_last_visit must be an object before ack can be banked")
    now = _now_ms() if now_ms is None else int(now_ms)
    current = load_state(state_path)
    if current.get("recovery") is not None:
        current = _empty_state(now)
    next_state = copy.deepcopy(current)
    try:
        next_state["pending_ack"] = merge_ack_cursor(next_state.get("pending_ack"), offered)
    except StateError:
        raise
    next_state["banked_reads"].append(
        {
            "banked_at_ms": now,
            "ack_cursor": copy.deepcopy(offered),
            "since_last_visit": copy.deepcopy(work),
        }
    )
    next_state["updated_at_ms"] = now
    next_state["recovery"] = None
    _validate_state(next_state)
    _atomic_write(state_path, next_state)
    return next_state


def replay_pending_inbox(state_path):
    state = load_state(state_path)
    if state.get("recovery") is not None:
        raise StateError("durable state recovery is required before inbox replay")
    rows = state.get("banked_reads") or []
    if not rows:
        raise StateError("no durably banked inbox work is pending")
    if len(rows) != 1:
        raise StateError("multiple durably banked inbox pages require explicit recovery; refusing to collapse them")
    row = rows[0]
    return {
        "ack_cursor": copy.deepcopy(row["ack_cursor"]),
        "since_last_visit": copy.deepcopy(row["since_last_visit"]),
        "replay_source": "durable_bank",
        "banked_at_ms": int(row["banked_at_ms"]),
    }


def ackable_cursor(state_path):
    state = load_state(state_path)
    if state.get("recovery") is not None:
        raise StateError("durable state recovery is required before acknowledgement")
    cursor = state.get("pending_ack")
    if cursor is None or not state.get("banked_reads"):
        raise StateError("no durably banked inbox cursor is pending")
    return copy.deepcopy(cursor)


def commit_verified_ack(state_path, acknowledged_cursor, now_ms=None):
    _validate_cursor(acknowledged_cursor, "acknowledged cursor")
    state = load_state(state_path)
    if state.get("recovery") is not None:
        raise StateError("cannot commit acknowledgement while recovery is required")
    remaining = [
        copy.deepcopy(row)
        for row in state.get("banked_reads", [])
        if not ack_cursor_leq(row["ack_cursor"], acknowledged_cursor)
    ]
    if not remaining:
        clear_state(state_path)
        return None

    floor = None
    for row in remaining:
        floor = merge_ack_cursor(floor, row["ack_cursor"])
    now = _now_ms() if now_ms is None else int(now_ms)
    next_state = copy.deepcopy(state)
    next_state["banked_reads"] = remaining
    next_state["pending_ack"] = floor
    next_state["updated_at_ms"] = now
    _validate_state(next_state)
    _atomic_write(state_path, next_state)
    return next_state


def clear_state(state_path):
    path = pathlib.Path(state_path)
    if path.exists():
        path.unlink()


def state_summary(state_path, now_ms=None):
    path = pathlib.Path(state_path)
    if not path.exists():
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "state": "EMPTY",
            "banked_reads": 0,
            "oldest_banked_at_ms": None,
            "oldest_age_seconds": None,
            "pending_ack": None,
            "recovery": None,
        }
    state = load_state(path)
    now = _now_ms() if now_ms is None else int(now_ms)
    recovery = copy.deepcopy(state.get("recovery"))
    if recovery is not None:
        state_name = "RECOVERY_REQUIRED"
    elif state.get("banked_reads"):
        state_name = "PENDING"
    else:
        state_name = "EMPTY"
    banked_times = [int(row["banked_at_ms"]) for row in state.get("banked_reads", [])]
    oldest = min(banked_times) if banked_times else None
    age = max(0, now - oldest) / 1000 if oldest is not None else None
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "state": state_name,
        "banked_reads": len(state.get("banked_reads", [])),
        "oldest_banked_at_ms": oldest,
        "oldest_age_seconds": age,
        "pending_ack": copy.deepcopy(state.get("pending_ack")),
        "recovery": recovery,
    }
