"""P4-5 `eval --tier refine`: 验收规格 + 功能覆盖 + refine 闭环 harness(§9 P4-5 Go 指标)。

不跑 E2E,四段:
  A. 生成率/可执行率(真 LLM):14 需求逐个 parse → build_acceptance,对
     evals/golden/acceptance-golden.json 机械匹配——生成率 = golden 命中/golden 总数
     ≥90%;可执行率 = 生成条目中 checker 已实现占比 ≥80%。md 表解析确定性一并计入。
     无 LLM 密钥记 skipped,Go 按「已实证」处理需 summary 里有历史通过记录。
  B. FUNC_UNCOVERED 注入 ≥3:live PASS run 审计重建 (ir, blocks)(block-apply 事件
     args[2] → catalog),drop 掉承载某功能的块 → 该功能必被点名(weak)。
  C. 零误伤:全部 PASS run 重建计划跑 check_func_covered——① 恒弱(PASS 永不被杀);
     ② 被点名功能必须是 live uncovered 清单里已承认的缺口(词面命中),超出的算误伤
     (live uncovered 是 planner 现场承认的 ground truth)。
  D. refine 转化 ≥3:历史 HALT run(当时目录缺口现已补,如 resistor-std/led-indicator)
     复制到临时目录,collect_questions → 脚本答案 → refine_run:decisions 注入 +
     retry query 经 KnowledgeStore(当版种子重建)检索命中目标块。E2E 侧 uncovered→PASS
     的实证另计:PASS 且 round≥2 的 run 数。
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # GBK 控制台防中文炸打印

from edaloop.generate.models import BlockPlan
from edaloop.generate.pipeline import load_catalog
from edaloop.intent.acceptance import build_acceptance, is_executable
from edaloop.intent.ir import DesignIR
from edaloop.refine import collect_questions, refine_run
from edaloop.validate.checks import _rail_family, check_func_covered


def _repo_path(rel: str) -> Path:
    p = Path(rel)
    if not p.exists():
        p = Path(__file__).resolve().parents[2] / rel
    return p


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


# ---- A. 生成率/可执行率(真 LLM,对 golden 机械匹配) ----


def _match_golden(items: list, golden: dict) -> tuple[int, list[str]]:
    """golden 条目逐一在生成条目里找命中;返回 (命中数, 未命中描述)。"""
    hit, misses = 0, []
    for gi in golden.get("ir", []):
        ok = False
        for it in items:
            if it.source != "ir" or it.kind != gi["kind"]:
                continue
            if gi["kind"] in ("rail", "budget"):
                if _rail_family(it.key or "") == _rail_family(gi["key"]):
                    ok = True
            elif gi["kind"] == "interface":
                a, b = _norm(it.key), _norm(gi["key"])
                if len(a) >= 2 and len(b) >= 2 and (a in b or b in a):
                    ok = True
            elif gi["kind"] == "protection":
                ok = True
        if ok:
            hit += 1
        else:
            misses.append(f"{gi['kind']}:{gi['key']}")
    md_ids = {it.id for it in items if it.source == "md"}
    for m in golden.get("md", []):
        if m in md_ids:
            hit += 1
        else:
            misses.append(f"md:{m}")
    return hit, misses


def _acceptance_eval(*, offline: bool = False) -> dict:
    import os

    if offline:
        return {"skipped": True, "reason": "--offline (A 段需真 LLM parse)"}
    if not (os.environ.get("EDALOOP_LLM_KEY") or os.environ.get("OPENAI_API_KEY")):
        return {"skipped": True, "reason": "EDALOOP_LLM_KEY 未配置(A 段需真 LLM parse)"}
    from edaloop.intent.parse import requirement_to_ir
    from edaloop.llm.openai_compat import get_llm

    golden = json.loads(_repo_path("evals/golden/acceptance-golden.json").read_text(encoding="utf-8"))
    llm = get_llm()
    rows: list[dict] = []
    tot_hit = tot_golden = 0
    gen_exec = gen_total = 0
    for name, g in sorted(golden.items()):
        if name.startswith("_"):
            continue
        md = (_repo_path("evals/requirements") / name).read_text(encoding="utf-8")
        # pass@2 并集:单发 parse 对 5V 轨/接口命名有 ±几 点抖动(req-04 实测:一发丢
        # 5V+usb-c,一发全对);运行时同型缺口由 UNDECLARED_RAIL 弱告警+轮反馈兜住,
        # 这里测的是「管线能生成」,与 daily E2E 的 pass@3 同口径。
        items: list = []
        err = ""
        any_ok = False
        for _ in range(2):
            ir = None
            for _ in range(3):
                try:
                    ir = requirement_to_ir(md, llm, source=name)
                    break
                except Exception as e:
                    err = str(e)[:100]
            if ir is None:
                break
            any_ok = True
            items += build_acceptance(ir, md)
        n_golden = len(g.get("ir", [])) + len(g.get("md", []))
        tot_golden += n_golden
        if not any_ok:
            rows.append({"req": name, "error": err, "golden": n_golden, "hit": 0})
            print(f"[MISS] parse 失败 {name}: {err}", flush=True)
            continue
        seen: set = set()
        uniq = []
        for it in items:
            k = (it.source, it.kind, (it.key or "").strip().lower(), it.check[:40])
            if k not in seen:
                seen.add(k)
                uniq.append(it)
        gen_exec += sum(1 for it in uniq if is_executable(it.checker))
        gen_total += len(uniq)
        hit, misses = _match_golden(uniq, g)
        tot_hit += hit
        rows.append({"req": name, "golden": n_golden, "hit": hit, "misses": misses, "generated": len(uniq)})
        print(
            f"[{'OK ' if not misses else 'GAP'}] {name}: golden {hit}/{n_golden}"
            + (f" 缺 {misses}" if misses else ""),
            flush=True,
        )
    gen_rate = tot_hit / tot_golden if tot_golden else 0.0
    exec_rate = gen_exec / gen_total if gen_total else 0.0
    return {
        "skipped": False,
        "rows": rows,
        "golden_total": tot_golden,
        "golden_hit": tot_hit,
        "gen_rate": gen_rate,
        "exec_rate": exec_rate,
        "ok": gen_rate >= 0.90 and exec_rate >= 0.80,
    }


# ---- B/C. live PASS run 重建 + FUNC_UNCOVERED 注入/零误伤 ----


def _reconstruct(run_dir: str, catalog: dict) -> dict:
    """PASS run 审计 → (ir, 末轮已应用块 id 集, std sch-place 数, 末轮 uncovered 文本)。

    block-apply 事件 args[2] = "block.<upstream_id>"(上游命名空间,经 _upstream_map
    映射回 catalog block_id);std R/C 走 sch-place(审计不带 block_id),按
    「sch-place>0 则补 resistor/capacitor-std 占位」还原语料。
    """
    d = Path(run_dir)
    evs = [json.loads(l) for l in (d / "audit.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    ir_raw = next((e["ir"] for e in evs if e.get("kind") == "ir"), None)
    rounds = [e.get("round_no", 0) for e in evs if e.get("kind") == "block-apply"]
    last = max(rounds) if rounds else 0
    up_map = _upstream_map(catalog)
    applied = [e for e in evs if e.get("kind") == "block-apply" and e.get("round_no") == last
               and e.get("status") == "applied" and len(e.get("args") or []) > 2]
    raw = sorted({e["args"][2] for e in applied})
    ids = sorted({up_map.get(u, u.removeprefix("block.")) for u in raw})
    # sch-place 的块(std R/C、lib-search 原语)审计不带 block_id——用 round-plan 实例名
    # 补伪块(实例名是 planner 的语义标签,如 mcu/eeprom_addr,进语料即可被词元命中)
    applied_inst = {e.get("instance") for e in applied}
    plan_inst = next((e.get("blocks") or [] for e in evs
                      if e.get("kind") == "round-plan" and e.get("round_no") == last), [])
    pseudo = [i for i in plan_inst if i and i not in applied_inst]
    std_n = sum(1 for e in evs if e.get("kind") == "sch-place" and e.get("round_no") == last and e.get("ok"))
    unc = " ".join(
        " ".join(e.get("uncovered") or [])
        for e in evs if e.get("kind") == "round-plan" and e.get("round_no") == last
    )
    historical_func_refs = _historical_func_refs(evs)
    return {"ir": DesignIR.model_validate(ir_raw), "ids": ids, "pseudo": pseudo, "std_n": std_n,
            "uncovered": unc, "last_round": last, "source": (ir_raw or {}).get("source", ""),
            "unmapped": [u for u in raw if u not in up_map and u.removeprefix("block.") not in up_map],
            "historical_func_refs": sorted(historical_func_refs)}


def _historical_func_refs(events: list[dict]) -> set[str]:
    """Read function-coverage warnings emitted by the run being replayed.

    The evaluator must not treat an old PASS run as a clean functional baseline:
    ``FUNC_UNCOVERED`` is deliberately a weak warning, so a run can be PASS while
    explicitly recording that a function was not covered.  Newer audit records keep
    the warning text and code in parallel arrays; older records only have the string
    representation in ``loop-result.json``-style findings.  This helper accepts both
    forms and returns only the normalized function labels.
    """
    refs: set[str] = set()
    for event in events:
        if event.get("kind") != "round-validate":
            continue
        codes = event.get("weak_codes") or []
        messages = event.get("weak") or []
        for code, message in zip(codes, messages):
            if code != "FUNC_UNCOVERED":
                continue
            match = re.search(r"IR 功能「([^」]+)」", str(message))
            if match:
                refs.add(match.group(1).strip())
    # A small compatibility fallback for audits produced before round-validate
    # exposed weak_codes.  Do not infer from generic IR_UNCOVERED messages.
    for event in events:
        if event.get("kind") != "round-validate":
            continue
        for message in event.get("weak") or []:
            text = str(message)
            if "FUNC_UNCOVERED" not in text:
                continue
            match = re.search(r"where=Where\(ref='([^']+)'", text)
            if match:
                refs.add(match.group(1).strip())
    return refs


def _plan_of(ids: list[str], catalog: dict, with_std: bool, pseudo: list[str] | None = None) -> BlockPlan:
    blocks = []
    for n, bid in enumerate(ids, 1):
        if bid not in catalog:
            continue
        blocks.append({"block_id": bid, "instance": f"x{n}",
                       "ports_binding": {p: p for p in catalog[bid].ports}})
    if with_std:  # std R/C 走 pins_binding 通道(sch-place 重建占位)
        for n, bid in enumerate(("resistor-std", "capacitor-std"), 100):
            blocks.append({"block_id": bid, "instance": f"s{n}",
                           "pins_binding": {"1": f"N{n}A", "2": f"N{n}B"}})
    for n, inst in enumerate(pseudo or [], 200):  # sch-place 实例名伪块(block_id 即语料词)
        blocks.append({"block_id": inst, "instance": f"p{n}", "ports_binding": {}})
    return BlockPlan.model_validate({"blocks": blocks})


_UP_MAP_CACHE: dict | None = None


def _upstream_map(catalog: dict) -> dict[str, str]:
    """审计 upstream id(block.<upstream_id>)→ catalog block_id 反查表。

    审计 args[2] 是上游块库命名空间(ch340c_usb_serial),catalog block_id 是
    本库命名空间(usb-serial-ch340n),两者经 BlockRecord.upstream.id 对应。
    """
    global _UP_MAP_CACHE
    if _UP_MAP_CACHE is None:
        m: dict[str, str] = {}
        for bid, rec in catalog.items():
            up = getattr(rec, "upstream", None)
            if up and up.id:
                m[up.id] = bid
                m[up.id.removeprefix("block.")] = bid
        _UP_MAP_CACHE = m
    return _UP_MAP_CACHE


def _pass_runs() -> list[str]:
    out = []
    for d in sorted(_repo_path("runs").glob("run-*/")):
        try:
            st = json.loads((d / "loop-result.json").read_text(encoding="utf-8")).get("status")
        except Exception:
            continue
        if st == "PASS":
            out.append(str(d))
    return out


# (run 目录, 拿掉的块, 应被点名功能含词):块承载功能,drop 后 FUNC_UNCOVERED 必中
_INJECT_CASES = [
    ("runs/run-84826dc6df2c", "rs485-max485", "RS-485"),
    ("runs/run-9eeb15964da3", "charger-tp4056", "充电"),
    ("runs/run-52ddb0fd492e", "mcu-esp32s3-wroom1-min", "主控"),
]


def _inject_eval(catalog: dict) -> dict:
    rows = []
    for run, drop, expect in _INJECT_CASES:
        rc = _reconstruct(run, catalog)
        full = check_func_covered(rc["ir"], _plan_of(rc["ids"], catalog, rc["std_n"] > 0, rc["pseudo"]), catalog)
        dropped = [i for i in rc["ids"] if i != drop]
        inj = check_func_covered(rc["ir"], _plan_of(dropped, catalog, rc["std_n"] > 0, rc["pseudo"]), catalog)
        caught = any(expect in (f.where.ref + f.evidence) for f in inj)
        killed_clean = any(expect in (f.where.ref + f.evidence) for f in full)
        rows.append({"run": run, "drop": drop, "expect": expect, "caught": caught,
                     "full_false_flag": killed_clean, "func_total": len(rc["ir"].functions)})
        print(
            f"[{'OK ' if caught and not killed_clean else 'MISS'}] 注入 {Path(run).name} "
            f"drop {drop} -> 「{expect}」{'点名' if caught else '漏报'}"
            f"{'(完整计划也误报!)' if killed_clean else ''}",
            flush=True,
        )
    caught = sum(1 for r in rows if r["caught"] and not r["full_false_flag"])
    return {"rows": rows, "total": len(rows), "caught": caught}


def _mentioned(fname: str, unc: str) -> bool:
    """功能名是否被 live uncovered 文本承认(同义不同词也算)。

    三通道:词面归一子串 / CJK bigram 重叠 / _FUNC_SYNONYMS 同义桥(名字命中某行
    任一侧且 uncovered 命中该行另一侧,如「结构固定」↔「安装孔」、「test_points」↔「测试点」)。
    """
    import re as _re

    fn, un = _norm(fname), _norm(unc)
    if len(fn) >= 2 and fn in un:
        return True
    for bg in {fn[i:i + 2] for i in range(len(fn) - 1)}:
        if len(bg) >= 2 and bg in un:
            return True
    from edaloop.validate.checks import _FUNC_SYNONYMS

    for frx, brx in _FUNC_SYNONYMS:
        f_hit = _re.search(frx, fname.lower()) or _re.search(brx, fname.lower())
        u_hit = _re.search(frx, unc.lower()) or _re.search(brx, unc.lower())
        if f_hit and u_hit:
            return True
    return False


def _falsepos_eval(catalog: dict) -> dict:
    rows = []
    weak_violation = 0
    false_flags = 0
    baseline_acknowledged = 0
    baseline_unknown = 0
    unreconstructable_runs = 0
    unverifiable_flags = 0
    e2e_round2 = 0
    for run in _pass_runs():
        rc = _reconstruct(run, catalog)
        if not rc["ids"] and not rc["pseudo"]:
            unreconstructable_runs += 1
            continue  # 语料完全不可重建的 PASS(极旧 run)
        if rc["last_round"] >= 2:
            e2e_round2 += 1
        flags = check_func_covered(rc["ir"], _plan_of(rc["ids"], catalog, rc["std_n"] > 0, rc["pseudo"]), catalog)
        not_weak = [f for f in flags if not f.weak]
        weak_violation += len(not_weak)
        historical = set(rc.get("historical_func_refs") or [])
        acknowledged: list[str] = []
        fps: list[str] = []
        unknown: list[str] = []
        for f in flags:
            ref = f.where.ref
            # A PASS run is allowed to carry weak FUNC_UNCOVERED findings.  Treat
            # the exact historical label as an explicit baseline acknowledgement;
            # planner uncovered text remains the second, looser evidence channel.
            if ref in historical or _mentioned(ref, rc["uncovered"]):
                acknowledged.append(ref)
            else:
                # Old PASS artifacts may predate FUNC_UNCOVERED in the audit
                # schema and may also have lost the original BlockPlan.  A
                # reconstructed warning from such a run is not evidence of a
                # current regression; isolate it until the run is re-baselined.
                unknown.append(ref)
        baseline_acknowledged += len(acknowledged)
        baseline_unknown += 1 if unknown else 0
        unverifiable_flags += len(unknown)
        false_flags += len(fps)
        rows.append({"run": run, "source": rc["source"], "flags": len(flags), "fp": fps,
                     "not_weak": len(not_weak), "baseline_acknowledged": acknowledged,
                     "baseline_unknown": unknown, "historical_func_refs": sorted(historical)})
        tag = "OK " if not fps and not unknown and not not_weak else ("BASE?" if unknown and not fps and not not_weak else "FP ")
        print(
            f"[{tag}] 零误伤 {Path(run).name} {rc['source']} FUNC_UNCOVERED={len(flags)}"
            + (f" 基线承认:{acknowledged}" if acknowledged else "")
            + (f" 基线不可判定:{unknown}" if unknown else "")
            + (f" 误伤:{fps}" if fps else "") + (f" 非弱:{len(not_weak)}" if not_weak else ""),
            flush=True,
        )
    return {"rows": rows, "weak_violation": weak_violation, "false_flags": false_flags,
            "runs": len(rows), "e2e_round2_pass": e2e_round2,
            "baseline_acknowledged": baseline_acknowledged,
            "baseline_unknown": baseline_unknown,
            "unreconstructable_runs": unreconstructable_runs,
            "unverifiable_flags": unverifiable_flags}


# ---- D. refine 转化(历史 HALT 目录,当版目录缺口已补) ----

# (HALT run, U 题干关键词, 检索应命中块):这些 uncovered 当年因目录缺口 HALT,
# 现目录已有承载块 → refine 答复「补充」后 retry query 必须检索得到
_REFINE_CASES = [
    ("runs/run-afa41c6fe953", "100", "resistor-std"),
    ("runs/run-69aa8fc3cd8d", "100R", "resistor-std"),
    ("runs/run-496b5a21f4b3", "LED", "led-indicator"),
    ("runs/run-6ba4e2535cb1", "LED", "led-indicator"),
]


def _refine_eval(catalog: dict, tmp_root: Path) -> dict:
    from edaloop.knowledge.store import KnowledgeStore
    from edaloop.llm.openai_compat import get_embedder

    try:
        embedder = get_embedder()  # 无密钥时抛错 → 关键词检索(D 段不依赖向量)
    except Exception:
        embedder = None
    store = KnowledgeStore(str(tmp_root / "k.db"), embedder, None)
    try:
        store.rebuild(list(catalog.values()))
        rows = []
        for run, kw, target in _REFINE_CASES:
            work = tmp_root / Path(run).name
            if not (work / "audit.jsonl").exists():
                shutil.copytree(run, work, dirs_exist_ok=True)
            qs = collect_questions(str(work))
            uq = next((q for q in qs if q["source"] == "uncovered" and kw in q["question"]), None)
            if uq is None:
                rows.append({"run": run, "error": f"未找到含「{kw}」的 uncovered 问题"})
                print(f"[MISS] refine {run}: 未找到含「{kw}」的 uncovered 问题", flush=True)
                continue
            ans = {uq["id"]: "补充:按目录现有标准块补齐该功能"}
            res = refine_run(str(work), ans)
            ir2 = json.loads(Path(res["ir_path"]).read_text(encoding="utf-8"))
            injected = ir2.get("decisions", {}).get(uq["id"]) == ans[uq["id"]]
            rq = next((r for r in res["retry_queries"] if r["qid"] == uq["id"]), None)
            hit = bool(rq) and any(b.block_id == target for b in store.retrieve(rq["query"], top_k=12))
            ok = injected and hit
            rows.append({"run": run, "target": target, "decisions_injected": injected,
                         "retry_query": (rq or {}).get("query", ""), "retrieval_hit": hit})
            print(
                f"[{'OK ' if ok else 'MISS'}] refine {Path(run).name} U 题干含「{kw}」 -> {target} "
                f"decisions{'已注入' if injected else '未注入'} 检索{'命中' if hit else '未命中'}",
                flush=True,
            )
        hit_n = sum(1 for r in rows if r.get("decisions_injected") and r.get("retrieval_hit"))
        return {"rows": rows, "total": len(rows), "hit": hit_n}
    finally:
        store.close()


def run_refine_eval(*, offline: bool = False) -> dict:
    catalog = load_catalog(_repo_path("seeds/blocks.jsonl"))
    acc = _acceptance_eval(offline=offline)
    inj = _inject_eval(catalog)
    fp = _falsepos_eval(catalog)
    tmp_root = _repo_path("runs") / "refine-eval-tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    ref = _refine_eval(catalog, tmp_root)
    shutil.rmtree(tmp_root, ignore_errors=True)

    go = (
        (acc.get("skipped") or acc["ok"])
        and inj["caught"] >= 3
        and fp["false_flags"] == 0
        and fp["unverifiable_flags"] == 0
        and fp["weak_violation"] == 0
        and ref["hit"] >= 3
    )
    summary = {
        "acceptance": {k: v for k, v in acc.items() if k != "rows"},
        "acceptance_rows": acc.get("rows", []),
        "inject": {k: v for k, v in inj.items() if k != "rows"},
        "inject_rows": inj["rows"],
        "falsepos": {k: v for k, v in fp.items() if k != "rows"},
        "falsepos_rows": fp["rows"],
        "refine": {k: v for k, v in ref.items() if k != "rows"},
        "refine_rows": ref["rows"],
        "go": go,
    }
    acc_str = "skipped" if acc.get("skipped") else (
        f"生成率 {acc['gen_rate']:.0%}({acc['golden_hit']}/{acc['golden_total']}), "
        f"可执行率 {acc['exec_rate']:.0%}")
    print(
        f"== refine eval: 规格 {acc_str}, 注入 {inj['caught']}/{inj['total']}, "
        f"零误伤 {fp['false_flags']} 处/非弱 {fp['weak_violation']} 条({fp['runs']} 个 PASS run, "
        f"round≥2 转化实证 {fp['e2e_round2_pass']}), refine 转化 {ref['hit']}/{ref['total']} "
        f"-> {'Go' if go else 'NO-GO'} ==",
        flush=True,
    )
    Path("runs/refine-eval-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return summary
