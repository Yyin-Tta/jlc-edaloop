from __future__ import annotations

import json

from edaloop.knowledge.models import BlockRecord, PartRef, UpstreamRef
from edaloop.knowledge.store import KnowledgeStore, _elec_digest
from edaloop.llm.fake import FakeEmbedding, FakeRerank


def _up(block_id: str) -> UpstreamRef:
    """P4-6④ 起不可落图块会被 retrieve 剔除——测试块默认给 upstream 占住 block-apply 通道。"""
    return UpstreamRef(id=f"block.{block_id}")


def _blocks() -> list[BlockRecord]:
    return [
        BlockRecord(
            block_id="charger-tp4056",
            name="TP4056 锂电充电管理",
            desc="单节锂电线性充电,充电电流 1A,双 LED 状态指示",
            tags=["tp4056", "充电", "charger"],
            parts=[PartRef(ref="TP4056", lcsc="C16581")],
            upstream=_up("charger-tp4056"),
        ),
        BlockRecord(
            block_id="ldo-ams1117",
            name="AMS1117-3.3 LDO 降压 3V3",
            desc="5V 转 3.3V 线性稳压 1A",
            tags=["ams1117", "ldo", "3v3"],
            parts=[PartRef(ref="AMS1117-3.3")],
            upstream=_up("ldo-ams1117"),
            ports=["VIN5V", "VOUT3V3", "GND"],
        ),
        BlockRecord(
            block_id="rs485-max485",
            name="MAX485 RS-485 收发器",
            desc="RS-485 收发,DE/RE 复用,120R 终端电阻",
            tags=["max485", "rs485"],
            upstream=_up("rs485-max485"),
        ),
        BlockRecord(
            block_id="mcu-stm32",
            name="STM32F103C8T6 最小系统",
            desc="8M 晶振 复位 BOOT0 SWD 排针,供电 3.3V",
            tags=["stm32f103", "最小系统"],
            upstream=_up("mcu-stm32"),
            ports=["3V3", "GND", "SWD"],
        ),
    ]


def _store(tmp_path, reranker=None) -> KnowledgeStore:
    store = KnowledgeStore(tmp_path / "kb.db", FakeEmbedding(), reranker)
    store.rebuild(_blocks())
    return store


def test_rebuild_count(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.count() == 4
    store.close()


def test_keyword_channel_hits_part_number(tmp_path) -> None:
    store = _store(tmp_path)
    results = store.retrieve("TP4056", top_k=3)
    assert results[0].block_id == "charger-tp4056"
    assert "keyword" in results[0].channels
    store.close()


def test_partnum_exact_ref_bonus(tmp_path) -> None:
    store = _store(tmp_path)
    results = store.retrieve("预留一路 RS-485,收发器用 MAX485 或 SP3485", top_k=2)
    assert results[0].block_id == "rs485-max485"
    assert "partnum" in results[0].channels
    store.close()


def test_partnum_contained_ref_bonus(tmp_path) -> None:
    store = _store(tmp_path)
    results = store.retrieve("压差和散热按 1117 规格算够,降到 3.3V", top_k=2)
    assert results[0].block_id == "ldo-ams1117"
    assert "partnum" in results[0].channels
    store.close()


def test_dense_channel_semantic(tmp_path) -> None:
    store = _store(tmp_path)
    results = store.retrieve("锂电池充电管理电路", top_k=2)
    assert results[0].block_id == "charger-tp4056"
    store.close()


def test_rerank_reorders(tmp_path) -> None:
    store = _store(tmp_path, reranker=FakeRerank())
    results = store.retrieve("RS-485 收发器 终端电阻", top_k=2)
    assert results[0].block_id == "rs485-max485"
    assert "rerank" in results[0].channels
    store.close()


def test_short_query_no_crash(tmp_path) -> None:
    store = _store(tmp_path)
    assert isinstance(store.retrieve("3V", top_k=2), list)
    store.close()


def test_provenance_preserved(tmp_path) -> None:
    store = _store(tmp_path)
    results = store.retrieve("TP4056", top_k=1)
    assert results[0].parts[0].lcsc == "C16581"
    assert results[0].rank == 1
    store.close()


# ---- P4-0②:electrical 列贯通 + 旧库 ALTER 迁移 ----


def test_electrical_roundtrip(tmp_path) -> None:
    from edaloop.knowledge.models import Electrical

    blocks = _blocks()
    blocks[1].electrical = Electrical(
        v_supply_min=4.5, v_supply_max=5.5, i_max=1.0, source="wmsc C6186"
    )
    store = KnowledgeStore(tmp_path / "kb.db", FakeEmbedding())
    store.rebuild(blocks)
    results = store.retrieve("AMS1117", top_k=1)
    el = results[0].electrical
    assert el is not None and el.i_max == 1.0 and el.source == "wmsc C6186"
    store.close()


def test_old_db_alter_migration(tmp_path) -> None:
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    # 旧 schema:无 electrical 列
    conn.execute(
        "CREATE TABLE blocks (rowid INTEGER PRIMARY KEY, block_id TEXT UNIQUE NOT NULL, name TEXT NOT NULL,"
        " desc TEXT NOT NULL, category TEXT DEFAULT 'general', tags TEXT DEFAULT '[]', parts TEXT DEFAULT '[]',"
        " ports TEXT DEFAULT '[]', provenance TEXT DEFAULT '', upstream TEXT DEFAULT '', lcsc TEXT DEFAULT '',"
        " pinout TEXT DEFAULT '')"
    )
    conn.execute(
        "INSERT INTO blocks(block_id, name, desc, lcsc) VALUES('legacy', '旧块', '迁移前数据', 'C1')"
    )
    conn.commit()
    conn.close()
    store = KnowledgeStore(db, FakeEmbedding())  # _ensure_schema 应触发 ALTER
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(blocks)")}
    assert "electrical" in cols
    store.rebuild(_blocks())  # 迁移后可正常重建
    assert store.count() == 4
    results = store.retrieve("TP4056", top_k=1)
    assert results[0].block_id == "charger-tp4056"
    store.close()


# ---- P4-6①+④:电气/类目入索引、rail 转换器通道、不可落图块过滤 ----


def test_rail_norm_variants() -> None:
    from edaloop.knowledge.store import _rails_in

    assert _rails_in("供电 3.3V 1A") == {"3v3"}
    assert _rails_in("3V3 与 GND") == {"3v3"}
    assert _rails_in("输入 12 V 输出") == {"12v"}
    assert _rails_in("1.8v/5.0V/3v7") == {"1v8", "5v", "3v7"}
    assert _rails_in("VIN5 GND SOT-223") == set()  # 无 v 尾随的裸数字不是轨
    assert _rails_in("S3 波特率 115200") == set()


def test_elec_digest_into_index_text(tmp_path) -> None:
    """contentless FTS 不存正文——digest 断言走 builder + 端到端关键词通道(词只存在于 digest)。"""
    from edaloop.knowledge.models import Electrical

    blocks = _blocks()
    blocks[1].electrical = Electrical(
        v_supply_min=4.5, v_supply_max=15.0, i_max=1.0, params={"vref": "1.25V"}, source="wmsc"
    )
    assert "v_supply_max 15.0v" in _elec_digest(blocks[1])
    store = KnowledgeStore(tmp_path / "kb.db", FakeEmbedding())
    store.rebuild(blocks)
    results = store.retrieve("1.25V", top_k=2)
    assert results and results[0].block_id == "ldo-ams1117" and "keyword" in results[0].channels
    store.close()


def test_rail_converter_channel_boosts_two_rail_block(tmp_path) -> None:
    """查询同时点名 5V 与 3.3V → ports 覆盖双轨的 LDO 拿 rail 通道加权,压过单轨 MCU。"""
    store = _store(tmp_path)
    results = store.retrieve("输入 5V 降压到 3.3V 给最小系统供电", top_k=4)
    ids = [r.block_id for r in results]
    assert ids[0] == "ldo-ams1117"
    top = results[0]
    assert "rail" in top.channels
    mcu = next(r for r in results if r.block_id == "mcu-stm32")
    assert "rail" not in mcu.channels  # 单轨命中不加分
    store.close()


def test_unplaceable_block_filtered_from_results(tmp_path) -> None:
    """battery-holder 类无 upstream/lcsc/std 通道的块:检索词高度命中也不得占 top-k。"""
    blocks = _blocks()
    blocks.append(
        BlockRecord(
            block_id="battery-18650-holder",
            name="18650 电池座",
            desc="锂电池 18650 电池座供电",
            tags=["18650", "电池座", "battery"],
        )
    )
    store = KnowledgeStore(tmp_path / "kb.db", FakeEmbedding())
    store.rebuild(blocks)
    results = store.retrieve("18650 电池座", top_k=5)
    assert "battery-18650-holder" not in [r.block_id for r in results]
    store.close()


def test_std_value_block_survives_filter(tmp_path) -> None:
    """resistor-std 无 upstream 无 lcsc,但走 std-value 通道——必须留在候选里。"""
    blocks = _blocks()
    blocks.append(
        BlockRecord(
            block_id="resistor-std", name="标准电阻", desc="阻值按标准表", tags=["resistor"]
        )
    )
    store = KnowledgeStore(tmp_path / "kb.db", FakeEmbedding())
    store.rebuild(blocks)
    results = store.retrieve("标准电阻", top_k=5)
    assert "resistor-std" in [r.block_id for r in results]
    store.close()


# ---- P4-6②/G16:datasheet 电气参数表 JOIN 回填 + 电气不兼容降权 ----


def test_datasheet_backfill_joins_by_ref(tmp_path) -> None:
    """datasheets 表(同库)按 parts.ref 命中 → 回填块缺失的 v_supply_min/max/i_max,不覆写既有值。"""
    from edaloop.knowledge.models import Electrical

    blocks = _blocks()
    blocks[1].electrical = Electrical(i_max=1.0, source="wmsc C6186")  # i_max 已有,不得被覆写
    store = KnowledgeStore(tmp_path / "kb.db", FakeEmbedding())
    store.rebuild(blocks)
    store.conn.execute(
        "CREATE TABLE IF NOT EXISTS datasheets (part TEXT PRIMARY KEY, pins TEXT NOT NULL)"
    )
    store.conn.execute(
        "INSERT INTO datasheets(part, pins) VALUES(?, ?)",
        ("AMS1117-3.3", json.dumps({"elec": [
            {"param": "Input voltage VIN", "min": "2.7", "typ": "", "max": "5.5", "unit": "V", "page": 3, "channel": "rule"},
            {"param": "Output current IOUT", "min": "", "typ": "", "max": "800", "unit": "mA", "page": 3, "channel": "rule"},
        ]})),
    )
    store.conn.commit()
    results = store.retrieve("AMS1117 3V3", top_k=1)
    el = results[0].electrical
    assert el is not None
    assert el.v_supply_min == 2.7  # 原本缺 → 回填
    assert el.v_supply_max == 5.5  # 原本缺 → 回填
    assert el.i_max == 1.0  # 已有值,不被 datasheet 覆写
    assert "datasheet:AMS1117-3.3" in el.source
    store.close()


def test_datasheet_backfill_prefix_join_for_variant_refs(tmp_path) -> None:
    """P5-1:库 ref 带变体后缀(AMS1117-3.3/CH340K)而 datasheet 部件名是裸名(AMS1117/CH340)
    ——精确等值零命中,前缀回退命中同 die 变体;source 记实际命中的 datasheet 部件名。"""
    from edaloop.knowledge.models import Electrical

    blocks = _blocks()
    blocks[1].electrical = Electrical()  # 全空,纯看回填
    store = KnowledgeStore(tmp_path / "kb.db", FakeEmbedding())
    store.rebuild(blocks)
    store.conn.execute(
        "CREATE TABLE IF NOT EXISTS datasheets (part TEXT PRIMARY KEY, pins TEXT NOT NULL)"
    )
    store.conn.execute(
        "INSERT INTO datasheets(part, pins) VALUES(?, ?)",
        ("AMS1117", json.dumps({"elec": [
            {"param": "Input voltage VIN", "min": "2.7", "typ": "", "max": "5.5", "unit": "V", "page": 1, "channel": "rule"},
        ]})),
    )
    store.conn.commit()
    results = store.retrieve("AMS1117 3V3", top_k=1)
    el = results[0].electrical
    assert el is not None
    assert el.v_supply_min == 2.7 and el.v_supply_max == 5.5
    assert "datasheet:AMS1117" in el.source  # 命中的是表里的裸名,不是设计 ref
    store.close()


def test_elec_deny_demotes_ldo_in_24v_direct_design(tmp_path) -> None:
    """24V 直入 3V3(无 5V 中间轨):ldo(max 15V, VIN_5V 无落点)被降权;5V 设计不误伤。"""
    from edaloop.knowledge.models import Electrical

    blocks = _blocks()
    blocks[1].category = "power"  # deny 通道只扫 power 类
    blocks[1].electrical = Electrical(v_supply_max=15.0, source="wmsc")
    blocks.append(
        BlockRecord(
            block_id="buck-wide-36v",
            name="宽压降压 36V",
            desc="36V 宽压输入降压",
            category="power",
            tags=["buck", "宽压"],
            upstream=_up("buck-wide-36v"),
            ports=["VIN12V", "5V", "GND"],
            electrical=Electrical(v_supply_max=36.0, source="wmsc"),
        )
    )
    store = KnowledgeStore(tmp_path / "kb.db", FakeEmbedding())
    store.rebuild(blocks)
    direct = store.retrieve("24V 输入直接降压输出 3.3V 供电 3V3", top_k=8)
    ldo = next((r for r in direct if r.block_id == "ldo-ams1117"), None)
    assert ldo is None or ("elec-deny" in ldo.channels and ldo.rank > 4)
    ok = store.retrieve("USB 5V 输入降压 3.3V 给单片机供电 3V3", top_k=8)
    ldo2 = next(r for r in ok if r.block_id == "ldo-ams1117")
    assert "elec-deny" not in ldo2.channels
    store.close()


# ---- P4-6③:案例第五通道 + 回写三护栏 ----


def _ir_stub(rails_text: str, interfaces: list[str], functions: list[str]):
    from edaloop.intent.ir import DesignIR, Function, Interface, Power, PowerRail

    return DesignIR(
        source="test",
        functions=[Function(name=n) for n in functions],
        interfaces=[Interface(type=t) for t in interfaces],
        power=Power(inputs=[rails_text], rails=[PowerRail(name="3V3", voltage=3.3)]),
    )


def test_case_channel_boosts_similar_group(tmp_path) -> None:
    """IR 指纹与案例相近(双轨+rs485 接口)→ 案例整组块拿 case 通道加权。"""
    from edaloop.knowledge.models import CaseDigest, CaseRecord

    store = _store(tmp_path)
    store.record_case(
        CaseRecord(
            case_id="case-t1",
            name="RS-485 工业节点",
            origin="seed:test",
            digest=CaseDigest(rails=["3v3", "5v"], interfaces=["rs485"], functions=["主控", "rs-485 通信"]),
            block_ids=["rs485-max485"],
        )
    )
    ir = _ir_stub("5V 端子输入", ["rs485", "uart"], ["主控", "RS-485 通信"])
    hit = next((r for r in store.retrieve("工业节点通信", top_k=4, ir=ir) if r.block_id == "rs485-max485"), None)
    assert hit is not None and "case" in hit.channels
    # 消融:同查询不带 ir → 通道结构性关闭(评测路径)
    hit2 = next((r for r in store.retrieve("工业节点通信", top_k=4) if r.block_id == "rs485-max485"), None)
    assert hit2 is None or "case" not in hit2.channels
    store.close()


def test_case_channel_dissimilar_ir_no_boost(tmp_path) -> None:
    """指纹不相近(轨不交+接口不交+功能不交)→ 不到阈值不给 case 通道。"""
    from edaloop.knowledge.models import CaseDigest, CaseRecord

    store = _store(tmp_path)
    store.record_case(
        CaseRecord(
            case_id="case-t2",
            name="音频功放",
            origin="seed:test",
            digest=CaseDigest(rails=["12v"], interfaces=["i2s"], functions=["音频放大"]),
            block_ids=["rs485-max485"],
        )
    )
    ir = _ir_stub("USB 5V 输入", ["rs485"], ["主控"])
    rs = store.retrieve("工业节点通信", top_k=4, ir=ir)
    hit = next((r for r in rs if r.block_id == "rs485-max485"), None)
    assert hit is None or "case" not in hit.channels
    store.close()


def test_record_case_hash_dedup(tmp_path) -> None:
    """digest+blocks 相同 → hash 撞车被忽略;改块集合 → 新案例。"""
    from edaloop.knowledge.models import CaseDigest, CaseRecord

    store = _store(tmp_path)
    c1 = CaseRecord(
        case_id="case-d1",
        name="节点A",
        origin="run:aaa",
        digest=CaseDigest(rails=["3v3"], interfaces=["rs485"], functions=["主控"]),
        block_ids=["rs485-max485", "mcu-stm32"],
    )
    assert store.record_case(c1) is True
    c1b = c1.model_copy(update={"case_id": "case-d1-again"})  # 同 digest+blocks,不同 case_id
    assert store.record_case(c1b) is False  # hash 去重
    c2 = c1.model_copy(update={"case_id": "case-d2", "block_ids": ["rs485-max485"]})
    assert store.record_case(c2) is True
    assert len(store.cases()) == 2
    store.close()


def test_case_table_survives_rebuild(tmp_path) -> None:
    """rebuild 只重建 blocks 族表——案例跨 rebuild 留存(生产库累积);eval 库每次新路径天然清空。"""
    from edaloop.knowledge.models import CaseDigest, CaseRecord

    store = _store(tmp_path)
    store.record_case(
        CaseRecord(
            case_id="case-r",
            name="节点",
            origin="seed:test",
            digest=CaseDigest(rails=["3v3"]),
            block_ids=["mcu-stm32"],
        )
    )
    store.rebuild(_blocks())
    assert len(store.cases()) == 1
    store.close()


def test_writeback_guards_eval_source_and_dedup(tmp_path, monkeypatch) -> None:
    """回写护栏:eval 源(req-*/evals)不写;生产源写入且 origin=run:<irid>;重复 hash 不入库。"""
    from edaloop.generate.audit import AuditLog
    from edaloop.generate.models import BlockPlan, PlannedBlock
    from edaloop.generate.pipeline import _maybe_record_case
    from edaloop.loop.controller import LoopResult

    monkeypatch.setattr("edaloop.generate.pipeline.get_embedder", lambda: FakeEmbedding())
    monkeypatch.setattr("edaloop.generate.pipeline.get_reranker", lambda: None)
    db = tmp_path / "wb.db"
    audit = AuditLog(tmp_path / "audit")
    ir = _ir_stub("5V 端子", ["rs485"], ["主控"])
    ir.id = "wbrun1"
    result = LoopResult(
        status="PASS",
        final_plan=BlockPlan(blocks=[PlannedBlock(block_id="rs485-max485", instance="rs1")]),
    )
    _maybe_record_case(ir, result, source="req-02-demo.md", dry_run=False, db_path=str(db), audit=audit)
    _maybe_record_case(ir, result, source="evals/xxx.md", dry_run=False, db_path=str(db), audit=audit)
    assert not db.exists()  # eval 源一律不落库
    _maybe_record_case(ir, result, source="customer-board-a.md", dry_run=False, db_path=str(db), audit=audit)
    _maybe_record_case(ir, result, source="customer-board-a.md", dry_run=False, db_path=str(db), audit=audit)
    store = KnowledgeStore(str(db), FakeEmbedding())
    cs = store.cases()
    assert len(cs) == 1  # 第二次同 digest+blocks 被 hash 去重
    assert cs[0].origin == "run:wbrun1" and cs[0].block_ids == ["rs485-max485"]
    store.close()
