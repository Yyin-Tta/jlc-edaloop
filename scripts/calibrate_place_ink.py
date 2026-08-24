"""place 通道墨迹标定(G33 页爆炸修复的硬前置)。

对每类代表器件:清 P1 → lib search → sch place → 逐 pin sch autoconnect
(netport 文字是翼展主体,必须带)→ 双口径读 bbox(clusters 组盒 +
sch list --include-bbox,人核后取大)→ 清页收尾。产物写入
.claude/measure-place-ink.json,stdout 直出 _PLACE_INK 字面量(粘进
compile.py)。

⚠ 会反复清前台页(默认 P1)——跑前确保工程 edaloop 已打开、前台是原理图页、
页上没有要保留的东西。

用法(仓库根):
  ./.venv/Scripts/python.exe scripts/calibrate_place_ink.py            # 全部 16 类
  ./.venv/Scripts/python.exe scripts/calibrate_place_ink.py --only nmos-2n7002,xtal-8m
  ./.venv/Scripts/python.exe scripts/calibrate_place_ink.py --dry-run  # 只打印命令不动真机
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from edaloop.generate.adapter import AdapterError, EasyedaAdapter  # noqa: E402
from edaloop.generate.compile import _pin_kind  # noqa: E402

DB = ROOT / "runs" / "knowledge.db"
OUT = ROOT / ".claude" / "measure-place-ink.json"
PAGE = "P1"
POS = (500, 450)  # 页中部落图,避图签带(y<198)与铁边距,只测 dx/dy 不测位置

# 16 类代表器件(按 _PLACE_INK_TIERS 三档 × 真实回归用量挑选;lcsc 以库内为准)
DEFAULT_BLOCKS = [
    # 档1:2 脚小件
    "resistor-std",  # C25744 10k 0402(stdparts 表,库里无 lcsc)
    "capacitor-std",  # C1525 100nF 0402(stdparts 表)
    "switch-6x6",
    "tvs-smaj5",
    "xtal-8m",
    "fuse-polyfuse",
    "diode-ss34",
    "terminal-kf301-2p",
    # 档2:3-9 脚中件
    "nmos-2n7002",
    "pmos-ao3401",
    "isolator-pc817",
    "isolated-dc-b0505s",
    "header-1x4",
    "esd-usblc6",
    # 档3:10+ 脚大件
    "usb-serial-ch340k",
    "mcu-stm32f103c8-min",
]
_DESIG = {  # 位号首字母(类别惯例,落图美观;autoconnect 用返回的真位号)
    "resistor-std": "R", "capacitor-std": "C", "switch-6x6": "SW", "tvs-smaj5": "D",
    "xtal-8m": "Y", "fuse-polyfuse": "F", "diode-ss34": "D", "terminal-kf301-2p": "J",
    "nmos-2n7002": "Q", "pmos-ao3401": "Q", "isolator-pc817": "U", "isolated-dc-b0505s": "U",
    "header-1x4": "J", "esd-usblc6": "U", "usb-serial-ch340k": "U", "mcu-stm32f103c8-min": "U",
}
_NET_PREFIX = "CALNET"  # 6 字符 + pin 号,贴近真实网名长度(RS485_A/MCU_TX 类)


def _load_pinouts(blocks: list[str]) -> dict[str, dict]:
    """库内 block_id → {lcsc, mpn, pinout};resistor/capacitor-std 走 stdparts 表。"""
    from edaloop.generate.stdparts import lookup

    out: dict[str, dict] = {}
    conn = sqlite3.connect(DB)
    try:
        for bid in blocks:
            row = conn.execute(
                "SELECT lcsc, pinout, parts FROM blocks WHERE block_id = ?", (bid,)
            ).fetchone()
            if row and row[1]:
                out[bid] = {
                    "lcsc": row[0] or "",
                    "mpn": (json.loads(row[2])[0]["ref"] if row[2] and json.loads(row[2]) else ""),
                    "pinout": json.loads(row[1]),
                }
    finally:
        conn.close()
    for bid, kind, val in (("resistor-std", "resistor", "10k"), ("capacitor-std", "capacitor", "100nF")):
        if bid in blocks and bid not in out:
            e = lookup(kind, val) or {}
            out[bid] = {"lcsc": e.get("lcsc", ""), "mpn": e.get("mpn", ""), "pinout": {"1": "A", "2": "B"}}
    return out


def _first_uuid(resp: dict) -> tuple[str, str]:
    res = resp.get("result", {}) or {}
    for r in res.get("components") or res.get("results") or []:
        lib = r.get("libraryUuid") or r.get("lib") or ""
        uuid = r.get("uuid") or r.get("deviceUuid") or ""
        if lib and uuid:
            return lib, uuid
    return "", ""


def _walk_boxes(node, desig: str):
    """任意 JSON 里递归找位号命中且带 minX/maxX/minY/maxY 的对象(sch list 口径)。"""
    found = []
    if isinstance(node, dict):
        keys = {k.lower(): v for k, v in node.items()}
        ident = str(keys.get("designator") or keys.get("name") or keys.get("ref") or "")
        if ident.upper().startswith(desig.upper()) and all(
            k in keys for k in ("minx", "maxx", "miny", "maxy")
        ):
            found.append({k: keys[k] for k in ("minX", "maxX", "minY", "maxY")})
        for v in node.values():
            found.extend(_walk_boxes(v, desig))
    elif isinstance(node, list):
        for v in node:
            found.extend(_walk_boxes(v, desig))
    return found


def _size(box: dict) -> tuple[int, int]:
    return int(round(box["maxX"] - box["minX"])), int(round(box["maxY"] - box["minY"]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", default="", help="逗号分隔 block_id 子集")
    ap.add_argument("--page", default=PAGE)
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--dry-run", action="store_true", help="只打印命令,不动真机")
    args = ap.parse_args()

    blocks = [b.strip() for b in args.only.split(",") if b.strip()] or DEFAULT_BLOCKS
    specs = _load_pinouts(blocks)
    missing = [b for b in blocks if b not in specs]
    if missing:
        print(f"[warn] 库内无 pinout,跳过: {missing}")

    if args.dry_run:
        for bid, s in specs.items():
            desig = f"{_DESIG.get(bid, 'U')}1"
            print(f"# {bid} lcsc={s['lcsc']} pins={len(s['pinout'])}")
            print(f"  sch clear --doc {args.page}")
            print(f"  lib search --query {s['lcsc']} --limit 3")
            print(f"  sch place --x {POS[0]} --y {POS[1]} --designator {desig}")
            for pin, name in s["pinout"].items():
                kind = _pin_kind(name or pin)
                print(f"  sch autoconnect --pin {desig}:{name or pin} --kind {kind} --net {_NET_PREFIX}{pin}")
            print(f"  sch clusters --json / sch list --include-bbox  (读 {args.page} 组盒)")
        return 0

    os.environ.setdefault("EDALOOP_PROJECT", "edaloop")
    adapter = EasyedaAdapter()

    # 环境门:版本 + 真实往返 + 前台须为原理图(warmup 的 sch clear 会被拒)
    adapter.check_version()
    rc, _, _ = adapter.run(["sch", "pages"])
    if rc != 0:
        print("[abort] sch pages 往返失败——daemon/连接器不在,先 easyeda daemon health")
        return 1
    rc, out, _ = adapter.run(["project", "doc"])
    try:
        dt = (json.loads(out).get("result") or {}).get("documentType", "")
    except ValueError:
        dt = ""
    if dt != "schematic":
        print(f"[abort] 前台不是原理图页(documentType={dt or '?'})——请在 EasyEDA 打开工程并切到原理图页")
        return 1

    results: dict[str, dict] = {}
    for bid, s in specs.items():
        print(f"\n== {bid} (lcsc={s['lcsc']}, pins={len(s['pinout'])}) ==")
        rec: dict = {"lcsc": s["lcsc"], "mpn": s["mpn"], "pins": len(s["pinout"])}
        results[bid] = rec
        adapter.run(["sch", "clear", "--doc", args.page])

        query = s["lcsc"] or s["mpn"]
        resp = adapter.run_json(["lib", "search", "--query", query, "--limit", "3"])
        lib, uuid = _first_uuid(resp)
        if not (lib and uuid):
            print(f"  [fail] lib search 无结果: {query}")
            rec["error"] = f"lib-search-empty:{query}"
            continue
        place = adapter.run_json([
            "sch", "place", "--lib", lib, "--uuid", uuid,
            "--x", str(POS[0]), "--y", str(POS[1]),
            "--designator", f"{_DESIG.get(bid, 'U')}1",
            "--doc", args.page,
        ])
        desig = ((place.get("result") or {}).get("component") or {}).get("designator", "")
        if not desig:
            print(f"  [fail] place 未返回位号: {json.dumps(place, ensure_ascii=False)[:200]}")
            rec["error"] = "place-no-designator"
            continue
        rec["designator"] = desig

        for pin, name in s["pinout"].items():
            kind = _pin_kind(name or pin)
            rc, _, err = adapter.run([
                "sch", "autoconnect", "--pin", f"{desig}:{name or pin}",
                "--kind", kind, "--net", f"{_NET_PREFIX}{pin}", "--doc", args.page,
            ])
            if rc != 0:
                print(f"  [warn] autoconnect {pin}({name}) rc={rc} {err[:80]}")

        # 口径1:clusters 组盒(netport 翼展在组盒内,这是页流真正消费的几何)
        box1 = None
        try:
            rep = adapter.run_json(["sch", "clusters", "--json", "--doc", args.page])
            for c in rep.get("clusters") or []:
                if str(c.get("designator", "")).upper() == desig.upper() and c.get("box"):
                    box1 = c["box"]
                    break
        except AdapterError as e:
            print(f"  [warn] clusters 读失败: {str(e)[:120]}")
        # 口径2:sch list --include-bbox(器件 bbox 并集,_INK_CELL 同源口径)
        box2 = None
        try:
            rc, out, _ = adapter.run(["sch", "list", "--include-bbox", "--doc", args.page])
            if rc == 0 and out.strip():
                cand = _walk_boxes(json.loads(out), desig)
                box2 = cand[0] if cand else None
        except (AdapterError, ValueError) as e:
            print(f"  [note] list --include-bbox 不可用: {str(e)[:80]}")

        for tag, box in (("cluster_box", box1), ("list_bbox", box2)):
            if box:
                rec[tag] = {k: float(box[k]) for k in ("minX", "minY", "maxX", "maxY")}
        dxs = []
        dys = []
        if box1:
            d1 = _size(box1)
            rec["cluster_size"] = d1
            dxs.append(d1[0]); dys.append(d1[1])
        if box2:
            d2 = _size(box2)
            rec["list_size"] = d2
            dxs.append(d2[0]); dys.append(d2[1])
        if dxs:
            # 拿不准取大(标定纪律:低估 = 页流过密 → overlap 修复链兜底,代价高)
            rec["chosen"] = (max(dxs), max(dys))
            rec["source"] = "cluster" if (box1 and (not box2 or _size(box1) == rec["chosen"])) else "list"
            print(f"  ok {desig} cluster={rec.get('cluster_size')} list={rec.get('list_size')} → chosen={rec['chosen']}")
        else:
            rec["error"] = rec.get("error", "no-box")
            print("  [fail] 双口径都没读到 bbox")
        adapter.run(["sch", "clear", "--doc", args.page])

    ok = {b: r["chosen"] for b, r in results.items() if r.get("chosen")}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(
        json.dumps(
            {
                "meta": {
                    "date": datetime.now(timezone.utc).isoformat(),
                    "page": args.page,
                    "pos": list(POS),
                    "net_prefix": _NET_PREFIX,
                    "note": "双口径 clusters 组盒 + sch list --include-bbox;chosen=取大;人核后可改",
                },
                "blocks": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n产物: {args.out}  ({len(ok)}/{len(results)} 类标定成功)")
    print("\n# ---- 粘进 compile.py 的 _PLACE_INK(标定成功的条目) ----")
    print("_PLACE_INK: dict[str, tuple[int, int]] = {")
    for b, (dx, dy) in ok.items():
        print(f'    "{b}": ({dx}, {dy}),')
    print("}")
    failed = [b for b, r in results.items() if not r.get("chosen")]
    if failed:
        print(f"\n[todo] 未标定成功(沿用分档缺省): {failed}")
    return 0 if len(ok) == len(results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
