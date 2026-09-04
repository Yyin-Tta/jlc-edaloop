"""Chainlit Web UI:聊天式驱动 edaloop 全链路(薄适配层,业务零侵入)。

架构(与 2026-08 会话层方案一致):
- 事件流:AuditLog.listener 是唯一 UI 事件源(stage_run(audit_listener=...) 注入),
  本文件只做「audit 事件 → 聊天流」翻译;listener 内吞异常,UI 崩了不拖垮 run。
- 会话目录:runs/ui/<session>/attachments(上传落盘,消息只引用路径)。
- 落图安全:默认 dry-run(不碰 EasyEDA 真机);真机模式首次使用时显式确认。
- 长任务:stage_run 跑在 asyncio.to_thread,audit 事件经 loop.call_soon_threadsafe
  回主循环流式更新(cl.Message.stream);run 中途无打断(弱门禁问题在 run 后经
  refine 通道收,同 CLI questions/refine 语义)。

启动:uv run --extra ui edaloop ui(工作目录须为仓库根,runs/ seeds/ 按相对路径解析)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import chainlit as cl

from edaloop.ui.router import classify
from edaloop.ui.session import format_event, save_attachment, session_dir

_WELCOME = (
    "**edaloop 已连接** —— 嘉立创 EDA 原理图设计 agent。\n\n"
    "**我能做什么**\n"
    "- 📋 **原理图设计**:发我一段需求描述(或上传 `.md`/`.txt` 需求文档),我会"
    "解析需求 → 检索电路块 → 生成原理图 → 自检迭代 → 交付网表/BOM/成本估算\n"
    "- 📄 **datasheet 入库**:上传器件手册 PDF,我提取引脚表与电气参数,扩充知识库\n"
    "- 🔁 **有问必答**:设计有歧义或未覆盖时,我会把问题逐条弹出来问你,答完自动用增量需求重跑\n\n"
    "当前为 **dry-run(只规划不出图)**;需要真机落图时我会先向你确认。"
)


def _cap_actions() -> list:
    """欢迎页常驻能力按钮(action_callback 驱动;点选与自由发消息并存)。"""
    return [
        cl.Action(name="cap_run", payload={}, icon="clipboard-list", label="提需求 · 开始设计"),
        cl.Action(name="cap_ingest", payload={}, icon="file-up", label="上传 datasheet 入库"),
        cl.Action(name="cap_mode", payload={}, icon="settings", label="切换落图模式"),
    ]

_DONE = "__done__"  # 工作线程结束哨兵(与业务事件区分)


def _sid() -> str:
    sid = cl.user_session.get("sid")
    if sid is None:
        sid = cl.context.session.id
        cl.user_session.set("sid", sid)
    return sid


def _llm():
    """分诊用 LLM(无 key/初始化失败返回 None,路由自动降级为快路径+全需求)。"""
    try:
        from edaloop.llm.openai_compat import get_llm

        return get_llm()
    except Exception:  # noqa: BLE001 - 分诊是增强,不挡主链路
        return None


async def _route_or_run(md: str, *, source: str) -> None:
    """文本入口统一路由:提问/闲聊回话,需求进 run(带需求文件的调用方不走这)。"""
    intent, reply = await asyncio.to_thread(classify, md, _llm())
    if intent == "chat":
        await cl.Message(content=reply or _WELCOME, actions=_cap_actions()).send()
        return
    await _do_run(md, source=source)


@cl.on_chat_start
async def on_chat_start() -> None:
    cl.user_session.set("sid", cl.context.session.id)
    cl.user_session.set("dry", None)  # None = 未确认,首次 run 时问
    session_dir(cl.context.session.id)
    await cl.Message(content=_WELCOME, actions=_cap_actions()).send()


@cl.action_callback("cap_run")
async def _on_cap_run(action: cl.Action) -> None:
    res = await cl.AskUserMessage(
        content="描述你的需求(功能、接口、电源、指标,随手写):", timeout=1800
    ).send()
    md = ((res or {}).get("output") or "").strip()
    if not md:
        await cl.Message(content="(没收到需求;也可以直接发消息,或上传 `.md`/`.txt` 文档)").send()
        return
    await _route_or_run(md, source="chat")


@cl.action_callback("cap_ingest")
async def _on_cap_ingest(action: cl.Action) -> None:
    await _handle_ingest()


@cl.action_callback("cap_mode")
async def _on_cap_mode(action: cl.Action) -> None:
    await _ask_dry()
    dry = cl.user_session.get("dry")
    await cl.Message(content=f"落图模式:{'真机落图' if not dry else 'dry-run(不出图)'}").send()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    # 附件一律落会话目录,消息里只引用路径
    req_files: list[Path] = []
    for el in message.elements or []:
        if not isinstance(el, cl.File):
            continue
        p = save_attachment(_sid(), el.name or "upload.bin", Path(el.path).read_bytes())
        if p.suffix.lower() in (".md", ".txt"):
            req_files.append(p)
        else:
            await cl.Message(content=f"已收文件:`{p.name}`(datasheet PDF 请用 `/ingest` 入库)").send()

    text = (message.content or "").strip()
    # 斜杠命令保留作隐藏快捷方式(主交互=欢迎页按钮+自由发消息,不再对外宣传)
    if text.startswith("/help"):
        await cl.Message(content=_WELCOME, actions=_cap_actions()).send()
        return
    if text.startswith("/ingest"):
        await _handle_ingest()
        return
    if text.startswith("/dry"):
        arg = text[len("/dry") :].strip().lower()
        if arg in ("on", "1", "true"):
            cl.user_session.set("dry", True)
        elif arg in ("off", "0", "false"):
            cl.user_session.set("dry", False)
        dry = cl.user_session.get("dry")
        await cl.Message(content=f"落图模式:{'真机落图' if dry else 'dry-run(不出图)'}").send()
        return

    source = req_files[0].name if req_files else "chat"
    md = req_files[0].read_text(encoding="utf-8") if req_files else text
    if req_files and text:
        await cl.Message(content=f"(以需求文件 `{source}` 为准,附带文本忽略)").send()
    if not md.strip():
        await cl.Message(content=_WELCOME, actions=_cap_actions()).send()
        return
    if req_files:
        await _do_run(md, source=source)  # 显式需求文件,不过分诊
        return
    await _route_or_run(md, source=source)


async def _ask_dry() -> bool:
    # 本版 Action 契约:name+payload 必填;选择结果从 res["name"] 读(2026-08-25 真机实证)
    res = await cl.AskActionMessage(
        content="落图模式?(之后可用 `/dry` 随时切换)",
        actions=[
            cl.Action(name="dry", payload={}, label="dry-run(不出图,推荐)", icon="bird"),
            cl.Action(name="real", payload={}, label="真机落图(需 EasyEDA 已连接)", icon="zap"),
        ],
        timeout=900,
    ).send()
    dry = not (res and res.get("name") == "real")
    cl.user_session.set("dry", dry)
    return dry


async def _do_run(
    md: str, *, source: str, ir_path: str | None = None, retry_queries: list[str] | None = None
) -> None:
    from edaloop.generate.pipeline import stage_run

    dry = cl.user_session.get("dry")
    if dry is None:
        dry = await _ask_dry()

    loop = asyncio.get_running_loop()
    q: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()

    def listener(kind: str, fields: dict) -> None:
        try:
            loop.call_soon_threadsafe(q.put_nowait, (kind, fields))
        except RuntimeError:  # 会话已关:run 在线程里继续跑完,事件丢弃
            pass

    def _run():
        try:
            return stage_run(
                md, source=source, dry_run=dry, ir_path=ir_path,
                retry_queries=retry_queries, audit_listener=listener,
            )
        finally:
            loop.call_soon_threadsafe(q.put_nowait, (_DONE, {}))

    fut = asyncio.ensure_future(asyncio.to_thread(_run))
    # 本版无 Message.stream():先 send() 落一条,事件到达后改 content 再 update()
    log = cl.Message(content=f"**run 启动**(source={source},{'dry-run' if dry else '真机'})\n")
    await log.send()
    finished = False
    while not finished:
        kind, fields = await q.get()
        if kind == _DONE:
            finished = True
            continue
        line = format_event(kind, fields)
        if not line:
            continue
        log.content += line + "\n"
        await log.update()
    try:
        ir, result = await fut
    except Exception as e:  # noqa: BLE001 - run 失败要回给用户而不是断 UI
        await cl.Message(content=f"run 失败:`{type(e).__name__}: {e}`").send()
        return

    delivery: dict = {}
    lr = Path(result.audit_dir) / "loop-result.json"
    if lr.exists():
        delivery = json.loads(lr.read_text(encoding="utf-8")).get("delivery") or {}
    status_line = {
        "PASS": f"✅ **PASS**({len(result.rounds)} 轮收敛)",
        "LAYOUT_REVIEW_REQUIRED": (
            "⚠️ **需人工布局复核**(电气/结构门禁已完成,页面可读性仍有问题)"
        ),
        "HALT": "⛔ **HALT**(同错升级人工)",
        "FAIL": f"❌ **FAIL**({len(result.rounds)} 轮未收敛)",
    }.get(result.status, result.status)
    failure_class = getattr(result, "failure_class", "")
    if failure_class:
        status_line += f"\n失败分类:`{failure_class}`"
    rounds_detail = "\n".join(
        f"- 轮 {r.round_no}:gate={r.gate_verdict},blocking={len([f for f in r.findings if not f.weak])}"
        + (f",halted={r.halted}" if r.halted else "")
        for r in result.rounds
    ) or "- (无轮记录)"

    elements: list = []
    for key, val in delivery.items():
        # svg 走下面的 Image 内嵌;计数字段(bom_total 等)不是路径
        if key in ("svg", "svg_pages") or not isinstance(val, str) or not Path(val).exists():
            continue
        elements.append(cl.File(name=Path(val).name, path=val))
    for svg in delivery.get("svg_pages") or ([delivery["svg"]] if delivery.get("svg") else []):
        if Path(svg).exists():
            elements.append(cl.Image(name=Path(svg).name, path=svg))

    await cl.Message(
        content=f"{status_line}\n{rounds_detail}\n审计:`{result.audit_dir}`", elements=elements
    ).send()
    if result.status != "PASS":
        await _offer_refine(md, source=source, audit_dir=result.audit_dir)


async def _offer_refine(md: str, *, source: str, audit_dir: str) -> None:
    """run 未全绿:收审计问题 → 弹选 → refine_run 产 IR-v2 → 问是否重跑。"""
    from edaloop.refine import collect_questions, refine_run

    questions = await asyncio.to_thread(collect_questions, audit_dir)
    if not questions:
        await cl.Message(content="审计里没有待决问题;FAIL 属生成/知识库侧,建议看审计目录。").send()
        return
    shown = questions[:8]
    if len(questions) > 8:
        await cl.Message(content=f"(只弹前 8 个,其余 {len(questions) - 8} 个用 `edaloop refine` 处理)").send()

    answers: dict[str, str] = {}
    for q in shown:
        ans = await _ask_question(q)
        if ans:
            answers[q["id"]] = ans
    if not answers:
        await cl.Message(content="未给答案,停在这里(审计已保留)。").send()
        return

    r = await asyncio.to_thread(refine_run, audit_dir, answers)
    await cl.Message(
        content=f"refine:applied={r['applied']},IR → {r['ir_revision']}"
        + (f";未答:{r['remaining']}" if r.get("remaining") else "")
    ).send()
    res = await cl.AskActionMessage(
        content="用 IR-v2(增量需求 + 二次检索)重跑?",
        actions=[
            cl.Action(name="rerun", payload={}, label="重跑", icon="rotate-ccw"),
            cl.Action(name="stop", payload={}, label="先不", icon="pause"),
        ],
        timeout=900,
    ).send()
    if res and res.get("name") == "rerun":
        retry = [x.get("query") for x in (r.get("retry_queries") or []) if x.get("query")]
        await _do_run(md, source=source, ir_path=r["ir_path"], retry_queries=retry)


async def _ask_question(q: dict) -> str | None:
    text = f"**[{q.get('id')}]** ({q.get('source', '')}) {q.get('question', '')}"
    opts = [o for o in (q.get("options") or []) if o]
    if not opts:
        res = await cl.AskUserMessage(content=text, timeout=900).send()
        return (res or {}).get("output") or None
    actions = [cl.Action(name=str(i), payload={}, label=o[:60]) for i, o in enumerate(opts[:4], 1)]
    actions.append(cl.Action(name="skip", payload={}, label="跳过(保持待定)"))
    res = await cl.AskActionMessage(content=text, actions=actions, timeout=900).send()
    if not res or res.get("name") == "skip":
        return None
    i = str(res.get("name"))
    return opts[int(i) - 1] if i.isdigit() and 1 <= int(i) <= min(len(opts), 4) else res.get("label")


async def _handle_ingest() -> None:
    from edaloop.ingest.pipeline import ingest_pdf
    from edaloop.llm.openai_compat import get_llm

    files = await cl.AskFileMessage(
        content="上传 datasheet PDF(可多选)",
        accept=[".pdf"],
        max_files=5,
        max_size_mb=40,  # 默认 2MB 装不下 datasheet(上限 100)
        timeout=900,
    ).send()
    if not files:
        await cl.Message(content="(未上传)").send()
        return
    llm = get_llm()
    for f in files:
        p = save_attachment(_sid(), f.name or "datasheet.pdf", Path(f.path).read_bytes())
        try:
            table, report = await asyncio.to_thread(ingest_pdf, str(p), llm)
        except Exception as e:  # noqa: BLE001 - 单文件失败不拦后续
            await cl.Message(content=f"`{p.name}`:FAILED - {e}").send()
            continue
        lines = [
            f"`{p.name}`:{table.part} pins={report.pin_count} "
            f"pages={report.evidence_pages} verdict={report.verdict}",
            *(f"- disagree: {d}" for d in report.disagreements[:3]),
            *(f"- suggest: {s.text[:60]}" for s in report.suggestions[:5]),
        ]
        await cl.Message(content="\n".join(lines)).send()
