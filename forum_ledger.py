import copy
import json
import os
import pathlib
import tempfile
import time
import uuid
from collections import Counter


LEDGER_SCHEMA_VERSION = 1
STATES = {"ATTEMPTED", "COMPLETED", "VERIFIED", "BLOCKED", "RECOVERABLE", "NOT_EXECUTED"}
UNRESOLVED_STATES = {"ATTEMPTED", "COMPLETED", "RECOVERABLE"}
TRANSITIONS = {
    "ATTEMPTED": {"COMPLETED", "RECOVERABLE", "NOT_EXECUTED"},
    "COMPLETED": {"VERIFIED", "RECOVERABLE"},
    "RECOVERABLE": {"VERIFIED"},
    "BLOCKED": set(),
    "NOT_EXECUTED": set(),
    "VERIFIED": set(),
}


class LedgerError(ValueError):
    pass


def _now_ms():
    return time.time_ns() // 1_000_000


def _empty_ledger(now_ms):
    return {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "created_at_ms": int(now_ms),
        "updated_at_ms": int(now_ms),
        "operations": [],
    }


def _validate_record(record, index=None):
    label = "operation" if index is None else f"operations[{index}]"
    if not isinstance(record, dict):
        raise LedgerError(f"{label} must be an object")
    required = (
        "id",
        "operation",
        "state",
        "created_at_ms",
        "updated_at_ms",
        "auto_replay_allowed",
        "intent",
        "evidence",
        "error",
    )
    missing = [key for key in required if key not in record]
    if missing:
        raise LedgerError(f"{label} missing fields: {', '.join(missing)}")
    if not isinstance(record["id"], str) or not record["id"]:
        raise LedgerError(f"{label}.id must be a non-empty string")
    if not isinstance(record["operation"], str) or not record["operation"]:
        raise LedgerError(f"{label}.operation must be a non-empty string")
    if record["state"] not in STATES:
        raise LedgerError(f"{label}.state is unsupported: {record['state']!r}")
    if record["auto_replay_allowed"] is not False:
        raise LedgerError(f"{label}.auto_replay_allowed must remain false")
    if not isinstance(record["intent"], dict):
        raise LedgerError(f"{label}.intent must be an object")
    if not isinstance(record["evidence"], dict):
        raise LedgerError(f"{label}.evidence must be an object")
    if record["error"] is not None and not isinstance(record["error"], str):
        raise LedgerError(f"{label}.error must be null or string")
    try:
        int(record["created_at_ms"])
        int(record["updated_at_ms"])
    except (TypeError, ValueError) as exc:
        raise LedgerError(f"{label} timestamps must be integers") from exc
    return record


def _validate_ledger(raw):
    if not isinstance(raw, dict):
        raise LedgerError("operation ledger must be a JSON object")
    if raw.get("schema_version") != LEDGER_SCHEMA_VERSION:
        raise LedgerError(f"unsupported operation ledger schema: {raw.get('schema_version')!r}")
    if not isinstance(raw.get("operations"), list):
        raise LedgerError("operation ledger operations must be a list")
    try:
        int(raw["created_at_ms"])
        int(raw["updated_at_ms"])
    except (KeyError, TypeError, ValueError) as exc:
        raise LedgerError("operation ledger timestamps must be integers") from exc
    seen = set()
    for index, record in enumerate(raw["operations"]):
        _validate_record(record, index)
        if record["id"] in seen:
            raise LedgerError(f"duplicate operation id: {record['id']}")
        seen.add(record["id"])
    return raw


def load_ledger(path):
    path = pathlib.Path(path)
    if not path.exists():
        return _empty_ledger(_now_ms())
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerError(f"could not read operation ledger: {exc}") from exc
    return _validate_ledger(raw)


def _atomic_write(path, ledger):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(ledger, indent=2, ensure_ascii=False) + "\n"
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


def _bounded_error(error):
    if error is None:
        return None
    return str(error)[:2000]


def begin_operation(path, operation, intent, now_ms=None, operation_id=None):
    if not isinstance(intent, dict):
        raise LedgerError("operation intent must be an object")
    now = _now_ms() if now_ms is None else int(now_ms)
    ledger = load_ledger(path)
    record = {
        "id": operation_id or uuid.uuid4().hex,
        "operation": str(operation),
        "state": "ATTEMPTED",
        "created_at_ms": now,
        "updated_at_ms": now,
        "auto_replay_allowed": False,
        "intent": copy.deepcopy(intent),
        "evidence": {},
        "error": None,
    }
    _validate_record(record)
    if any(row["id"] == record["id"] for row in ledger["operations"]):
        raise LedgerError(f"duplicate operation id: {record['id']}")
    ledger["operations"].append(record)
    ledger["updated_at_ms"] = now
    _atomic_write(path, ledger)
    return copy.deepcopy(record)


def record_blocked(path, operation, intent, error, now_ms=None, operation_id=None):
    now = _now_ms() if now_ms is None else int(now_ms)
    ledger = load_ledger(path)
    record = {
        "id": operation_id or uuid.uuid4().hex,
        "operation": str(operation),
        "state": "BLOCKED",
        "created_at_ms": now,
        "updated_at_ms": now,
        "auto_replay_allowed": False,
        "intent": copy.deepcopy(intent),
        "evidence": {},
        "error": _bounded_error(error),
    }
    _validate_record(record)
    ledger["operations"].append(record)
    ledger["updated_at_ms"] = now
    _atomic_write(path, ledger)
    return copy.deepcopy(record)


def transition_operation(path, operation_id, state, evidence=None, error=None, now_ms=None):
    if state not in STATES:
        raise LedgerError(f"unsupported operation state: {state}")
    now = _now_ms() if now_ms is None else int(now_ms)
    ledger = load_ledger(path)
    target = next((row for row in ledger["operations"] if row["id"] == operation_id), None)
    if target is None:
        raise LedgerError(f"unknown operation id: {operation_id}")
    current = target["state"]
    if state not in TRANSITIONS[current]:
        raise LedgerError(f"invalid operation transition: {current} -> {state}")
    if evidence:
        if not isinstance(evidence, dict):
            raise LedgerError("operation evidence must be an object")
        target["evidence"].update(copy.deepcopy(evidence))
    target["state"] = state
    target["updated_at_ms"] = now
    target["error"] = _bounded_error(error)
    target["auto_replay_allowed"] = False
    ledger["updated_at_ms"] = now
    _validate_ledger(ledger)
    _atomic_write(path, ledger)
    return copy.deepcopy(target)



def get_operation(path, operation_id):
    ledger = load_ledger(path)
    target = next((row for row in ledger["operations"] if row["id"] == operation_id), None)
    if target is None:
        raise LedgerError(f"unknown operation id: {operation_id}")
    return copy.deepcopy(target)


def record_reconciliation(
    path,
    operation_id,
    delivery_state,
    evidence=None,
    error=None,
    now_ms=None,
):
    if delivery_state not in {"recovered_match", "contradiction", "unknown"}:
        raise LedgerError(f"unsupported reconciliation state: {delivery_state}")
    now = _now_ms() if now_ms is None else int(now_ms)
    ledger = load_ledger(path)
    target = next((row for row in ledger["operations"] if row["id"] == operation_id), None)
    if target is None:
        raise LedgerError(f"unknown operation id: {operation_id}")
    if target["state"] not in UNRESOLVED_STATES:
        raise LedgerError(f"operation is already terminal: {target['state']}")
    history = target["evidence"].setdefault("reconciliation_history", [])
    if not isinstance(history, list):
        raise LedgerError("reconciliation_history must be a list")
    entry = {
        "at_ms": now,
        "delivery_state": delivery_state,
        "evidence": copy.deepcopy(evidence or {}),
        "error": _bounded_error(error),
    }
    history.append(entry)
    if len(history) > 20:
        del history[:-20]
    target["updated_at_ms"] = now
    target["auto_replay_allowed"] = False
    ledger["updated_at_ms"] = now
    _validate_ledger(ledger)
    _atomic_write(path, ledger)
    return copy.deepcopy(target)


def mark_reconciled_verified(path, operation_id, evidence=None, now_ms=None):
    now = _now_ms() if now_ms is None else int(now_ms)
    ledger = load_ledger(path)
    target = next((row for row in ledger["operations"] if row["id"] == operation_id), None)
    if target is None:
        raise LedgerError(f"unknown operation id: {operation_id}")
    if target["state"] not in UNRESOLVED_STATES:
        raise LedgerError(f"operation is already terminal: {target['state']}")
    history = target["evidence"].setdefault("reconciliation_history", [])
    if not isinstance(history, list):
        raise LedgerError("reconciliation_history must be a list")
    entry = {
        "at_ms": now,
        "delivery_state": "recovered_match",
        "evidence": copy.deepcopy(evidence or {}),
        "error": None,
    }
    history.append(entry)
    if len(history) > 20:
        del history[:-20]
    if evidence:
        target["evidence"].update(copy.deepcopy(evidence))
    target["state"] = "VERIFIED"
    target["updated_at_ms"] = now
    target["error"] = None
    target["auto_replay_allowed"] = False
    ledger["updated_at_ms"] = now
    _validate_ledger(ledger)
    _atomic_write(path, ledger)
    return copy.deepcopy(target)


def operations_summary(path, now_ms=None):
    path = pathlib.Path(path)
    if not path.exists():
        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "operations": 0,
            "unresolved": 0,
            "states": {},
            "oldest_unresolved_age_seconds": None,
            "unresolved_operations": [],
        }
    ledger = load_ledger(path)
    now = _now_ms() if now_ms is None else int(now_ms)
    states = Counter(row["state"] for row in ledger["operations"])
    unresolved = [row for row in ledger["operations"] if row["state"] in UNRESOLVED_STATES]
    oldest = min((int(row["created_at_ms"]) for row in unresolved), default=None)
    age = max(0, now - oldest) / 1000 if oldest is not None else None
    return {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "operations": len(ledger["operations"]),
        "unresolved": len(unresolved),
        "states": dict(sorted(states.items())),
        "oldest_unresolved_age_seconds": age,
        "unresolved_operations": [copy.deepcopy(row) for row in unresolved],
    }
