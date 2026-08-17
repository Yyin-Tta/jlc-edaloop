from __future__ import annotations

import json

from edaloop.knowledge.models import BlockRecord, PartRef
from edaloop.knowledge.store import KnowledgeStore
from edaloop.llm.fake import FakeEmbedding, FakeRerank


def _blocks() -> list[BlockRecord]:
    return [
        BlockRecord(
            block_id="charger-tp4056",
            name="TP4056 锂电充电管理",
            desc="单节锂电线性充电,充电电流 1A,双 LED 状态指示",
            tags=["tp4056", "充电", "charger"],
            parts=[PartRef(ref="TP4056", lcsc="C16581")],
        ),
        BlockRecord(
            block_id="ldo-ams1117",
            name="AMS1117-3.3 LDO 降压 3V3",
            desc="5V 转 3.3V 线性稳压 1A",
            tags=["ams1117", "ldo", "3v3"],
            parts=[PartRef(ref="AMS1117-3.3")],
        ),
        BlockRecord(
            block_id="rs485-max485",
            name="MAX485 RS-485 收发器",
            desc="RS-485 收发,DE/RE 复用,120R 终端电阻",
            tags=["max485", "rs485"],
        ),
        BlockRecord(
            block_id="mcu-stm32",
            name="STM32F103C8T6 最小系统",
            desc="8M 晶振 复位 BOOT0 SWD 排针",
            tags=["stm32f103", "最小系统"],
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
