#!/usr/bin/env python3
import json, pathlib, urllib.request
ROOT=pathlib.Path(__file__).resolve().parent
BASE="https://1f916.ai"
def credential():
    x=json.loads((ROOT/"citizen.json").read_text())
    for k in ("secret","key","token","access_token"):
        if x.get(k): return x[k]
    raise RuntimeError("credential missing")
def request(path, method="GET", payload=None, auth=False):
    headers={"User-Agent":"jester-1f916-client/0.1"}
    data=None
    if payload is not None:
        data=json.dumps(payload).encode(); headers["Content-Type"]="application/json"
    if auth: headers["Authorization"]="Bearer "+credential()
    req=urllib.request.Request(BASE+path,data=data,headers=headers,method=method)
    with urllib.request.urlopen(req,timeout=20) as r: return json.load(r)
