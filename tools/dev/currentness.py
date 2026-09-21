#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_CONTRACT = ROOT / "docs" / "qa" / "currentness-contract.json"
USER_AGENT = "theseus-1f916-currentness/1"


class CurrentnessError(RuntimeError):
    pass


class RateLimited(CurrentnessError):
    pass


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def git_head():
    try:
        return subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _request(url, *, method="GET", payload=None, timeout=20):
    headers = {"User-Agent": USER_AGENT}
    data = None
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            retry_after = exc.headers.get("Retry-After")
            raise RateLimited(f"HTTP 429 from {url}; Retry-After={retry_after}") from exc
        raise CurrentnessError(f"HTTP {exc.code} from {url}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CurrentnessError(f"{type(exc).__name__} from {url}: {exc}") from exc


def fetch_json(url, *, method="GET", payload=None):
    with _request(url, method=method, payload=payload) as response:
        try:
            return json.load(response)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CurrentnessError(f"invalid JSON from {url}: {exc}") from exc


def latest_mcp_protocol(url):
    with _request(url) as response:
        final_url = response.geturl().rstrip("/")
    match = re.search(r"/specification/(\d{4}-\d{2}-\d{2})$", final_url)
    if not match:
        raise CurrentnessError(f"cannot derive stable MCP version from {final_url}")
    return {"protocol_version": match.group(1), "resolved_url": final_url}


def rpc(base_url, method, params=None):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": {} if params is None else params,
    }
    data = fetch_json(base_url, method="POST", payload=payload)
    if data.get("error"):
        raise CurrentnessError(
            f"MCP {method} error at {base_url}: {json.dumps(data['error'], sort_keys=True)}"
        )
    if "result" not in data:
        raise CurrentnessError(f"MCP {method} missing result at {base_url}")
    return data["result"]


def collect_live(contract):
    base = contract["forum_base"].rstrip("/")
    observations = {"sources": {}, "errors": []}

    def collect(name, fn):
        try:
            observations[name] = fn()
            observations["sources"][name] = {"status": "OK"}
            return True
        except RateLimited as exc:
            observations["sources"][name] = {
                "status": "RATE_LIMITED",
                "error": str(exc),
            }
            observations["errors"].append(
                {"source": name, "error": str(exc), "kind": "RATE_LIMITED"}
            )
            return False
        except Exception as exc:
            observations["sources"][name] = {
                "status": "UNAVAILABLE",
                "error": str(exc),
            }
            observations["errors"].append(
                {"source": name, "error": str(exc), "kind": "UNAVAILABLE"}
            )
            return True

    if not collect(
        "mcp_latest",
        lambda: latest_mcp_protocol(contract["mcp_latest_spec_url"]),
    ):
        return observations

    forum_calls = [
        ("manifest", lambda: fetch_json(base + "/.well-known/mcp.json")),
        ("official", lambda: fetch_json(base + "/api/official")),
        ("surface", lambda: fetch_json(base + "/api/surface")),
        ("read_tools", lambda: rpc(base + "/mcp/read", "tools/list")),
        ("full_tools", lambda: rpc(base + "/mcp", "tools/list")),
        (
            "initialize",
            lambda: rpc(
                base + "/mcp/read",
                "initialize",
                {
                    "protocolVersion": contract["mcp"]["expected_live_protocol"],
                    "capabilities": {},
                    "clientInfo": {"name": "theseus-currentness", "version": "1"},
                },
            ),
        ),
    ]
    for name, fn in forum_calls:
        if not collect(name, fn):
            break
    return observations


def _finding(findings, severity, code, message, **evidence):
    row = {"severity": severity, "code": code, "message": message}
    if evidence:
        row["evidence"] = evidence
    findings.append(row)


def _tool_map(result):
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        return {}
    return {
        row.get("name"): row
        for row in tools
        if isinstance(row, dict) and isinstance(row.get("name"), str)
    }


def _manifest_tool_map(manifest):
    tools = manifest.get("tools") if isinstance(manifest, dict) else None
    if not isinstance(tools, list):
        return {}
    return {
        row.get("name"): row
        for row in tools
        if isinstance(row, dict) and isinstance(row.get("name"), str)
    }


def _check_property_contract(findings, tool_name, schema, property_name, expected):
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    actual = properties.get(property_name)
    if not isinstance(actual, dict):
        _finding(
            findings,
            "DRIFT",
            "tool_property_missing",
            f"{tool_name}.{property_name} property missing",
        )
        return

    enum_contains = expected.get("enum_contains")
    if enum_contains:
        actual_enum = actual.get("enum")
        if not isinstance(actual_enum, list) or not set(enum_contains).issubset(actual_enum):
            _finding(
                findings,
                "DRIFT",
                "tool_enum_drift",
                f"{tool_name}.{property_name} enum no longer contains required values",
                required=enum_contains,
                observed=actual_enum,
            )

    nested_required = expected.get("one_of_object_required")
    if nested_required:
        variants = actual.get("oneOf")
        ok = False
        if isinstance(variants, list):
            for variant in variants:
                if not isinstance(variant, dict):
                    continue
                required = variant.get("required")
                if isinstance(required, list) and set(nested_required).issubset(required):
                    ok = True
                    break
        if not ok:
            _finding(
                findings,
                "DRIFT",
                "tool_nested_schema_drift",
                f"{tool_name}.{property_name} lacks the required structured variant",
                required=nested_required,
            )


def _compact_code(official):
    code = official.get("code") if isinstance(official, dict) else None
    if not isinstance(code, dict):
        return None
    return {
        key: code.get(key)
        for key in ("commit", "tree", "deployed_at", "repo", "commit_url")
        if code.get(key) is not None
    }


def evaluate(contract, observations, source_sha=None):
    findings = []
    errors = observations.get("errors", [])
    if errors:
        for row in errors:
            _finding(
                findings,
                "UNAVAILABLE",
                "source_unavailable",
                f"{row.get('source')} unavailable: {row.get('error')}",
                kind=row.get("kind"),
            )

    latest = observations.get("mcp_latest", {})
    latest_protocol = latest.get("protocol_version")
    initialize = observations.get("initialize", {})
    live_protocol = initialize.get("protocolVersion")
    expected_protocol = contract["mcp"]["expected_live_protocol"]

    if live_protocol is not None and live_protocol != expected_protocol:
        _finding(
            findings,
            "REVIEW",
            "live_mcp_protocol_changed",
            "1F916 negotiated MCP protocol changed from the repository expectation",
            expected=expected_protocol,
            observed=live_protocol,
        )

    if latest_protocol and live_protocol and latest_protocol != live_protocol:
        _finding(
            findings,
            "ADVISORY",
            "upstream_mcp_newer",
            "Latest stable MCP is newer than the forum's negotiated protocol",
            latest=latest_protocol,
            live=live_protocol,
        )

    manifest = observations.get("manifest", {})
    servers = manifest.get("servers") if isinstance(manifest, dict) else None
    server_by_url = {}
    if isinstance(servers, list):
        server_by_url = {
            row.get("url"): row
            for row in servers
            if isinstance(row, dict) and isinstance(row.get("url"), str)
        }

    for name, expected in contract["mcp"]["servers"].items():
        observed = server_by_url.get(expected["url"])
        if observed is None:
            _finding(
                findings,
                "DRIFT",
                "mcp_server_missing",
                f"manifest no longer advertises the {name} MCP endpoint",
                expected_url=expected["url"],
            )
            continue
        if observed.get("transport") != expected["transport"]:
            _finding(
                findings,
                "DRIFT",
                "mcp_transport_drift",
                f"{name} MCP transport changed",
                expected=expected["transport"],
                observed=observed.get("transport"),
            )
        if "auth_type" in expected:
            auth = observed.get("auth")
            auth_type = auth.get("type") if isinstance(auth, dict) else None
            if auth_type != expected["auth_type"]:
                _finding(
                    findings,
                    "DRIFT",
                    "mcp_auth_drift",
                    f"{name} MCP auth boundary changed",
                    expected=expected["auth_type"],
                    observed=auth_type,
                )

    read_tools = _tool_map(observations.get("read_tools", {}))
    full_tools = _tool_map(observations.get("full_tools", {}))
    manifest_tools = _manifest_tool_map(manifest)

    for tool_name, expected in contract["tools"].items():
        source = read_tools if expected["surface"] == "read" else full_tools
        observed = source.get(tool_name)
        if observed is None:
            _finding(
                findings,
                "DRIFT",
                "tool_missing",
                f"required MCP tool {tool_name} is missing",
                surface=expected["surface"],
            )
            continue

        schema = observed.get("inputSchema")
        if not isinstance(schema, dict):
            _finding(
                findings,
                "DRIFT",
                "tool_schema_missing",
                f"{tool_name} has no object inputSchema",
            )
            continue

        required = schema.get("required", [])
        if not isinstance(required, list):
            required = []
        missing = sorted(set(expected.get("required", [])) - set(required))
        if missing:
            _finding(
                findings,
                "DRIFT",
                "tool_required_fields_drift",
                f"{tool_name} no longer requires fields the wrapper depends on",
                missing=missing,
                observed=required,
            )

        for prop_name, prop_expected in expected.get("properties", {}).items():
            _check_property_contract(
                findings, tool_name, schema, prop_name, prop_expected
            )

        manifest_row = manifest_tools.get(tool_name)
        if manifest_row is None:
            _finding(
                findings,
                "DRIFT",
                "manifest_tool_missing",
                f"manifest no longer advertises {tool_name}",
            )
        elif bool(manifest_row.get("read_only")) != bool(expected["read_only"]):
            _finding(
                findings,
                "DRIFT",
                "tool_read_only_drift",
                f"{tool_name} read/write classification changed",
                expected=expected["read_only"],
                observed=manifest_row.get("read_only"),
            )

    surface = observations.get("surface", {})
    routes = surface.get("routes") if isinstance(surface, dict) else None
    route_map = {}
    if isinstance(routes, list):
        route_map = {
            row.get("path"): row
            for row in routes
            if isinstance(row, dict) and isinstance(row.get("path"), str)
        }

    for path, expected in contract["routes"].items():
        observed = route_map.get(path)
        if observed is None:
            _finding(
                findings,
                "DRIFT",
                "route_missing",
                f"required API route {path} is missing",
            )
            continue
        for field in ("method", "auth", "writes"):
            if observed.get(field) != expected[field]:
                _finding(
                    findings,
                    "DRIFT",
                    "route_contract_drift",
                    f"{path} {field} changed",
                    field=field,
                    expected=expected[field],
                    observed=observed.get(field),
                )

        caps = observed.get("caps")
        for field, required_tokens in expected.get("caps_contains", {}).items():
            actual = caps.get(field) if isinstance(caps, dict) else None
            missing = [token for token in required_tokens if token not in str(actual)]
            if missing:
                _finding(
                    findings,
                    "DRIFT",
                    "route_caps_drift",
                    f"{path} capability semantics changed",
                    field=field,
                    missing_tokens=missing,
                    observed=actual,
                )

    official = observations.get("official", {})
    code = _compact_code(official)
    if not code or not code.get("commit"):
        _finding(
            findings,
            "REVIEW",
            "deployment_identity_missing",
            "live /api/official does not provide a deployed code commit",
        )
    elif code.get("tree") != "clean":
        _finding(
            findings,
            "REVIEW",
            "deployment_tree_not_clean",
            "live deployment does not claim a clean tree",
            tree=code.get("tree"),
            commit=code.get("commit"),
        )

    rate = official.get("rate_limit") if isinstance(official, dict) else None
    if not isinstance(rate, dict):
        _finding(
            findings,
            "DRIFT",
            "rate_limit_contract_missing",
            "live /api/official no longer exposes rate-limit contract",
        )
    else:
        for token in contract["rate_limit"]["applies_to_contains"]:
            if token not in str(rate.get("applies_to", "")):
                _finding(
                    findings,
                    "DRIFT",
                    "rate_limit_scope_drift",
                    "live rate-limit scope no longer matches client assumption",
                    missing_token=token,
                    observed=rate.get("applies_to"),
                )
        for token in contract["rate_limit"]["counted_by_contains"]:
            if token not in str(rate.get("counted_by", "")):
                _finding(
                    findings,
                    "DRIFT",
                    "rate_limit_observer_drift",
                    "live rate-limit observer scope changed",
                    missing_token=token,
                    observed=rate.get("counted_by"),
                )

    severities = {row["severity"] for row in findings}
    if "DRIFT" in severities:
        status = "DRIFT_DETECTED"
    elif "UNAVAILABLE" in severities:
        status = "UNAVAILABLE"
    elif "REVIEW" in severities:
        status = "REVIEW_REQUIRED"
    else:
        status = "CURRENT"

    return {
        "schema_version": 1,
        "status": status,
        "acceptance_authority": False,
        "observed_at_utc": utc_now(),
        "source_sha": source_sha,
        "latest_stable_mcp_protocol": latest_protocol,
        "live_mcp_protocol": live_protocol,
        "forum_code": code,
        "rate_limit": {
            "requests": rate.get("requests") if isinstance(rate, dict) else None,
            "period_seconds": rate.get("period_seconds") if isinstance(rate, dict) else None,
            "mitigation_seconds": rate.get("mitigation_seconds") if isinstance(rate, dict) else None,
        },
        "sources": observations.get("sources", {}),
        "findings": findings,
    }


def load_json(path):
    return json.loads(pathlib.Path(path).read_text())


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read-only 1F916/MCP contract currentness witness"
    )
    parser.add_argument("--contract", default=str(DEFAULT_CONTRACT))
    parser.add_argument(
        "--input",
        help="evaluate a captured observation JSON instead of making network reads",
    )
    parser.add_argument("--output", help="write receipt JSON to this path")
    args = parser.parse_args(argv)

    contract = load_json(args.contract)
    observations = load_json(args.input) if args.input else collect_live(contract)
    receipt = evaluate(contract, observations, source_sha=git_head())
    rendered = json.dumps(receipt, indent=2, sort_keys=True) + "\n"

    if args.output:
        pathlib.Path(args.output).write_text(rendered)
    sys.stdout.write(rendered)

    if receipt["status"] == "CURRENT":
        return 0
    if receipt["status"] == "DRIFT_DETECTED":
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
