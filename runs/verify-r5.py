# -*- coding: utf-8 -*-
"""run-0cdc61dd3eea 验收:逐页 pin 网 + clusters 本体几何。只读。"""
import json
import subprocess
import sys

WIN = "4a69ece8-70d6-4278-8659-54f61f5c02cb"
PAPER = (12.0, 12.0, 1158.0, 813.0)  # 图框内边线再缩 2


def call(args: list[str]) -> dict:
    p = subprocess.run(["easyeda", *args, "--window", WIN], capture_output=True,
                       text=True, encoding="utf-8", timeout=120)
    try:
        data, _ = json.JSONDecoder().raw_decode(p.stdout)
    except ValueError:
        print(f"!! parse fail: {args} rc={p.returncode} out[:200]={p.stdout[:200]}")
        return {}
    return data.get("result", data)


mode = sys.argv[1] if len(sys.argv) > 1 else "pins"
pages = sys.argv[2].split(",") if len(sys.argv) > 2 else [f"P{i}" for i in range(2, 9)]

if mode == "pins":
    for page in pages:
        data = call(["sch", "list", "--page", page, "--include-pins"])
        print(f"== {page} ==")
        for c in data.get("components", []):
            if c.get("componentType") != "part":
                continue
            pins = c.get("pins") or []
            nets = [(str(p.get("pinNumber")), p.get("net") or "") for p in pins]
            empty = [n for n, net in nets if not net]
            x = c.get("x"); y = c.get("y")
            print(f"  {c.get('designator')} @({x},{y}) {len(pins)}p empty={len(empty)}"
                  + (f" 空:{empty}" if empty else ""))
elif mode == "clusters":
    for page in pages:
        data = call(["sch", "clusters", "--json", "--doc", page])
        clusters = data.get("clusters") if isinstance(data, dict) else data
        print(f"== {page} ==")
        for cl in clusters or []:
            box = cl.get("box") or cl.get("bounds") or {}
            parts = [p.get("designator") if isinstance(p, dict) else p
                     for p in (cl.get("parts") or cl.get("members") or [])]
            print(f"  {cl.get('name') or cl.get('label') or '?'} box={box} parts={parts[:12]}")
