#!/usr/bin/env python3
import json
import os
import pathlib
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
BASE = "https://1f916.ai"
RUNTIME_CREDENTIAL = pathlib.Path("/workspace/agents/jester/1f916/citizen.json")


def _credential_from_file(path):
    data = json.loads(pathlib.Path(path).read_text())
    for key in ("secret", "key", "token", "access_token"):
        if data.get(key):
            return data[key]
    raise RuntimeError(f"credential missing in {path}")


def credential(env=None, root=None, runtime_path=None):
    env = os.environ if env is None else env
    root = ROOT if root is None else pathlib.Path(root)
    runtime_path = RUNTIME_CREDENTIAL if runtime_path is None else pathlib.Path(runtime_path)

    direct = env.get("JESTER_FORUM_CREDENTIAL")
    if direct:
        return direct

    explicit_file = env.get("JESTER_FORUM_CREDENTIAL_FILE")
    if explicit_file:
        path = pathlib.Path(explicit_file)
        if not path.is_file():
            raise FileNotFoundError(path)
        return _credential_from_file(path)

    local = root / "citizen.json"
    if local.is_file():
        return _credential_from_file(local)
    if runtime_path.is_file():
        return _credential_from_file(runtime_path)

    raise FileNotFoundError(
        f"forum credential unavailable: checked {local} and {runtime_path}"
    )


def request(path, method="GET", payload=None, auth=False):
    headers = {"User-Agent": "jester-1f916-client/0.1"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if auth:
        headers["Authorization"] = "Bearer " + credential()
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)
