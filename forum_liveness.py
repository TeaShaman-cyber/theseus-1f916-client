import copy
import json
import os
import pathlib
import tempfile
import time


SCHEMA_VERSION = 1
DEFAULT_STALE_AFTER_INTERVALS = 2.0


class LivenessError(ValueError):
    pass


def _now_ms():
    return time.time_ns() // 1_000_000


def _positive_seconds(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _nonnegative_int(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def classify_pulse(data, configured_interval_s=None, stale_after_intervals=DEFAULT_STALE_AFTER_INTERVALS):
    if not isinstance(data, dict):
        raise LivenessError("pulse data must be an object")
    you = data.get("you")
    if not isinstance(you, dict):
        raise LivenessError("authenticated pulse has no you object")

    try:
        multiplier = float(stale_after_intervals)
    except (TypeError, ValueError) as exc:
        raise LivenessError("stale_after_intervals must be a positive number") from exc
    if multiplier <= 0:
        raise LivenessError("stale_after_intervals must be a positive number")

    declared = _positive_seconds(you.get("declared_interval_s"))
    configured = _positive_seconds(configured_interval_s)
    server_default = _positive_seconds(data.get("poll_interval_s"))
    if declared is not None:
        interval_s = declared
        interval_source = "declared_interval_s"
    elif configured is not None:
        interval_s = configured
        interval_source = "client_config"
    elif server_default is not None:
        interval_s = server_default
        interval_source = "server_default_poll_interval_s"
    else:
        interval_s = None
        interval_source = None

    now_ms = _nonnegative_int(data.get("now"))
    last_ack_at = _nonnegative_int(you.get("last_ack_at"))
    age_ms = _nonnegative_int(you.get("last_ack_age_ms"))
    if age_ms is None and now_ms is not None and last_ack_at is not None:
        age_ms = max(0, now_ms - last_ack_at)

    watermark = you.get("watermark")
    if watermark not in {"behind", "current"}:
        watermark = None

    threshold_ms = (
        int(interval_s * 1000 * multiplier)
        if interval_s is not None
        else None
    )

    if watermark == "current" and age_ms is not None:
        freshness = "FRESH"
    elif (
        watermark == "behind"
        and age_ms is not None
        and threshold_ms is not None
        and age_ms > threshold_ms
    ):
        freshness = "STALE_CURSOR"
    elif (
        watermark == "behind"
        and age_ms is not None
        and threshold_ms is not None
    ):
        freshness = "FRESH"
    else:
        freshness = "UNKNOWN"

    return {
        "schema_version": SCHEMA_VERSION,
        "read_freshness": freshness,
        "wake_signal_usable": freshness == "FRESH",
        "cursor_mode": you.get("cursor_mode"),
        "server_last_ack_at_ms": last_ack_at,
        "server_last_ack_age_ms": age_ms,
        "watermark": watermark,
        "interval_s": interval_s,
        "interval_source": interval_source,
        "stale_after_intervals": multiplier,
        "threshold_ms": threshold_ms,
        "has_new_for_you": you.get("has_new_for_you"),
    }


def _atomic_write(path, payload):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
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
            handle.write(text)
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


def load(path):
    path = pathlib.Path(path)
    if not path.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "read_freshness": "UNKNOWN",
            "wake_signal_usable": False,
            "cursor_mode": None,
            "last_verified_ack_at_ms": None,
        }
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise LivenessError(f"could not read liveness state: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        raise LivenessError("unsupported liveness state schema")
    return raw


def observe_pulse(
    path,
    data,
    *,
    observed_at_ms=None,
    configured_interval_s=None,
    stale_after_intervals=DEFAULT_STALE_AFTER_INTERVALS,
):
    observed = _now_ms() if observed_at_ms is None else int(observed_at_ms)
    classification = classify_pulse(
        data,
        configured_interval_s=configured_interval_s,
        stale_after_intervals=stale_after_intervals,
    )
    previous = load(path)
    record = {
        **classification,
        "observed_at_ms": observed,
        "last_verified_ack_at_ms": previous.get("last_verified_ack_at_ms"),
    }
    _atomic_write(path, record)
    return copy.deepcopy(record)


def record_verified_ack(path, *, cursor_mode, now_ms=None):
    observed = _now_ms() if now_ms is None else int(now_ms)
    current = load(path)
    current["schema_version"] = SCHEMA_VERSION
    current["cursor_mode"] = cursor_mode
    current["last_verified_ack_at_ms"] = observed
    current["read_freshness"] = "UNKNOWN_AFTER_VERIFIED_ACK"
    current["wake_signal_usable"] = False
    current["observed_at_ms"] = observed
    _atomic_write(path, current)
    return copy.deepcopy(current)


def summary(path, now_ms=None):
    record = load(path)
    observed = _nonnegative_int(record.get("observed_at_ms"))
    now = _now_ms() if now_ms is None else int(now_ms)
    result = copy.deepcopy(record)
    result["evidence_age_ms"] = max(0, now - observed) if observed is not None else None
    return result
