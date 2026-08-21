"""P4-0②:LCSC wmsc paramVOList → BlockRecord.electrical 回填工具。

两步走,人工终审原则(ADR-0001)不自动落库:
  1) fetch:抓 wmsc 详情,按品类映射 paramNameEn → 电气字段,产出 proposal JSONL(含 raw_params 供人工核对)
  2) apply:人工审核/修剪 proposal 后回写 seeds/blocks.jsonl,再跑 `edaloop-cli seed` 重建知识库

wmsc 字段实测(2026-08 spike):STM32 "Voltage - Supply"="2V~3.6V";AMS1117 "Output Current"="1A";
ULN2003 "Ic"="500mA";C9580(SS34) params 为空——空/失败一律记入 proposal 的 errors,不静默跳过。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

WMSC_URL = "https://wmsc.lcsc.com/ftps/wm/product/detail?productCode={code}"

# paramNameEn(小写精确)→ 电气槽位;品类歧义键(如 Ic 仅驱动/晶体管语境才作 i_max)单列
_V_RANGE_KEYS = ("voltage - supply", "supply voltage", "operating voltage", "voltage - rated", "voltage - input")
_I_MAX_KEYS = ("output current", "current - output", "collector current", "ic", "continuous drain current")
_I_TYP_KEYS = ("operating supply current", "supply current", "quiescent current", "operating current")

_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")


def parse_v_range(text: str) -> tuple[float, float] | None:
    """"2V~3.6V"/"1.71V~3.6V"/"5V" → (min,max);无法解析返回 None。"""
    nums = _NUM_RE.findall(text.replace(" ", ""))
    if not nums:
        return None
    vals = [float(n) for n in nums]
    if len(vals) == 1:
        return (vals[0], vals[0])
    lo, hi = min(vals), max(vals)
    return (lo, hi)


def parse_current(text: str) -> float | None:
    """"1A"/"500mA"/"1.5 A" → 安培数值;无法解析返回 None。"""
    t = text.replace(" ", "").lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*ma\b", t)
    if m:
        return float(m.group(1)) / 1000.0
    m = re.search(r"(\d+(?:\.\d+)?)\s*ua\b", t)
    if m:
        return float(m.group(1)) / 1_000_000.0
    m = re.search(r"(\d+(?:\.\d+)?)\s*a\b", t)
    if m:
        return float(m.group(1))
    return None


def fetch_params(lcsc: str, *, timeout: float = 15.0) -> dict[str, str] | None:
    """wmsc 详情 paramVOList → {paramNameEn: paramValue};请求失败/无参数返回 None/{}。"""
    import httpx

    try:
        resp = httpx.get(WMSC_URL.format(code=lcsc), timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None
    detail = data.get("result") or data
    param_list = detail.get("paramVOList") or []
    return {p.get("paramNameEn", ""): p.get("paramValue", "") for p in param_list if p.get("paramNameEn")}


def map_params(params: dict[str, str], category: str = "") -> dict:
    """品类感知映射:params → {v_supply_min/max, i_max, i_typ};未消费键记入 unmapped。"""
    out: dict = {}
    unmapped: list[str] = []
    cat = (category or "").lower()
    for key, val in params.items():
        k = key.strip().lower()
        if not val or val in ("-", ""):
            continue
        if k in _V_RANGE_KEYS:
            rng = parse_v_range(val)
            if rng:
                out.setdefault("v_supply_min", rng[0])
                out.setdefault("v_supply_max", rng[1])
        elif k in _I_MAX_KEYS:
            # "Output Current" 仅电源类作 i_max;其余语境不强映射,留 unmapped 人工判
            if "power" in cat or k in ("collector current", "ic", "continuous drain current"):
                cur = parse_current(val)
                if cur is not None:
                    out.setdefault("i_max", cur)
            else:
                unmapped.append(f"{key}={val}")
        elif k in _I_TYP_KEYS:
            cur = parse_current(val)
            if cur is not None:
                out.setdefault("i_typ", cur)
        else:
            unmapped.append(f"{key}={val}")
    out["unmapped"] = unmapped
    return out


def cmd_fetch(args: argparse.Namespace) -> int:
    from edaloop.knowledge.models import BlockRecord

    blocks = [
        BlockRecord.model_validate(json.loads(line))
        for line in Path(args.seeds).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    only = set(args.only.split(",")) if args.only else None
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n_ok = n_empty = n_err = 0
    with out_path.open("w", encoding="utf-8") as fh:
        for b in blocks:
            if only and b.block_id not in only:
                continue
            codes: list[tuple[str, str]] = []  # (lcsc, 用途标注)
            if b.lcsc:
                codes.append((b.lcsc, "block"))
            codes += [(p.lcsc, f"part:{p.ref}") for p in b.parts if p.lcsc]
            if not codes:
                continue
            lcsc, use = codes[0]
            params = fetch_params(lcsc)
            if params is None:
                n_err += 1
                fh.write(json.dumps({"block_id": b.block_id, "lcsc": lcsc, "error": "wmsc 请求失败", "proposed": {}}, ensure_ascii=False) + "\n")
                continue
            if not params:
                n_empty += 1
                fh.write(json.dumps({"block_id": b.block_id, "lcsc": lcsc, "error": "paramVOList 为空(常见于被动件)", "raw_params": {}, "proposed": {}}, ensure_ascii=False) + "\n")
                continue
            mapped = map_params(params, b.category)
            unmapped = mapped.pop("unmapped", [])
            proposal = {"block_id": b.block_id, "lcsc": lcsc, "lcsc_use": use, "raw_params": params, "proposed": mapped, "unmapped": unmapped}
            fh.write(json.dumps(proposal, ensure_ascii=False) + "\n")
            n_ok += 1
            if unmapped:
                print(f"[unmapped] {b.block_id}: {unmapped[:3]}", file=sys.stderr)
            time.sleep(args.sleep)
    print(f"fetched ok={n_ok} empty={n_empty} err={n_err} -> {out_path}")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    from edaloop.knowledge.models import BlockRecord, Electrical

    seeds_path = Path(args.seeds)
    lines = seeds_path.read_text(encoding="utf-8").splitlines()
    blocks = [BlockRecord.model_validate(json.loads(line)) for line in lines if line.strip()]
    by_id = {b.block_id: b for b in blocks}
    n_applied = 0
    for line in Path(args.proposal).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        b = by_id.get(item.get("block_id"))
        if b is None:
            continue
        proposed = item.get("proposed") or {}
        fields = {k: v for k, v in proposed.items() if v is not None}
        if not fields:
            continue
        lcsc = item.get("lcsc", "")
        src = f"wmsc {lcsc} paramVOList" if lcsc else "proposal"
        new_el = Electrical(source=src, **fields)
        if b.electrical is not None and not args.force:
            merged = b.electrical.model_dump()
            merged.update({k: v for k, v in new_el.model_dump().items() if v not in (None, "") and k != "source"})
            if src not in merged.get("source", ""):
                merged["source"] = f"{merged.get('source', '')}; {src}".strip("; ")
            b.electrical = Electrical(**merged)
        else:
            b.electrical = new_el
        n_applied += 1
    tmp = seeds_path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for b in blocks:
            fh.write(json.dumps(json.loads(b.model_dump_json()), ensure_ascii=False) + "\n")
    tmp.replace(seeds_path)
    print(f"applied electrical to {n_applied}/{len(blocks)} blocks -> {seeds_path};请运行 edaloop-cli seed 重建知识库")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="wmsc 电气参数回填(fetch 出 proposal,人工审核后 apply)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_fetch = sub.add_parser("fetch", help="抓 wmsc paramVOList,产出 proposal JSONL(不写 seeds)")
    p_fetch.add_argument("--seeds", default="seeds/blocks.jsonl")
    p_fetch.add_argument("--out", default="runs/electrical-proposal.jsonl")
    p_fetch.add_argument("--only", default="", help="逗号分隔 block_id 白名单,空=全部")
    p_fetch.add_argument("--sleep", type=float, default=0.6, help="请求间隔秒(限速)")
    p_fetch.set_defaults(func=cmd_fetch)
    p_apply = sub.add_parser("apply", help="把审核后的 proposal 回写 seeds(缺省只填空槽,--force 覆盖)")
    p_apply.add_argument("--proposal", default="runs/electrical-proposal.jsonl")
    p_apply.add_argument("--seeds", default="seeds/blocks.jsonl")
    p_apply.add_argument("--force", action="store_true", help="覆盖已有非空电气字段")
    p_apply.set_defaults(func=cmd_apply)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
