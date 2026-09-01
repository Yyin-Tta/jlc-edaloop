"""packer 装箱纯函数测试:无重叠不变量、页数对照、oversize、带/组亲和、空输入。

v0.6.11:间隙函数化(piece 100 / shelf 按行高 35% 自适应 100-200),固定 200 的
断言改为按 packer 同一公式推导;新增 module 组亲和与 PACK-3 扁宽块判据。
"""

from __future__ import annotations

import itertools
import random

from edaloop.generate.packer import (
    BAND,
    TITLEBLOCK_KEEPOUT,
    Cell,
    _shelf_gap,
    pack,
)

BAND_W = BAND[2] - BAND[0]
BAND_H = BAND[3] - BAND[1]


def _cells_random(rng: random.Random, n: int) -> list[Cell]:
    out = []
    for i in range(n):
        out.append(Cell(
            f"b{i}",
            rng.uniform(60, BAND_W * 0.6),
            rng.uniform(40, BAND_H * 0.6),
            rng.randint(0, 2),
            rng.choice(["upstream", "place"]),
        ))
    return out


def _assert_no_overlap(cells: list[Cell], res) -> None:
    boxes = {}
    for c in cells:
        p, x, y = res.placements[c.name]
        boxes[c.name] = (p, x, y, x + c.w, y + c.h)
        # 页内必须在带内(oversize 左上锚页除外,test_all_in_band 单独豁免)
        assert 0 <= p < res.pages, f"{c.name} 页号越界"
    for a, b in itertools.combinations(boxes, 2):
        pa, ax1, ay1, ax2, ay2 = boxes[a]
        pb, bx1, by1, bx2, by2 = boxes[b]
        if pa != pb:
            continue
        sep = ax2 <= bx1 or bx2 <= ax1 or ay2 <= by1 or by2 <= ay1
        assert sep, f"同页重叠: {a} {boxes[a]} vs {b} {boxes[b]}"


def test_empty_input():
    res = pack([])
    assert res.placements == {} and res.pages == 0 and res.oversize == []


def test_no_overlap_invariant_random_500():
    rng = random.Random(42)
    for _ in range(500):
        cells = _cells_random(rng, rng.randint(1, 12))
        res = pack(cells)
        _assert_no_overlap(cells, res)
        assert set(res.placements) == {c.name for c in cells}


def test_all_in_band():
    rng = random.Random(7)
    cells = _cells_random(rng, 10)
    res = pack(cells)
    for c in cells:
        _, x, y = res.placements[c.name]
        if c.name in res.oversize:
            continue  # oversize 左上锚块超高/超宽时如实出带
        assert BAND[0] <= x and x + c.w <= BAND[2], f"{c.name} x 出带"
        assert BAND[1] <= y and y + c.h <= BAND[3], f"{c.name} y 出带"


def test_oversize_gets_own_page_top_left():
    # vehicle 实测 864×1384:高溢出任何 A4 带 → 独占页左上锚(2026-08-25 用户定案:
    # 不再居中——独占块居中目检就是"第一块放中间")+ oversize 记录
    cells = [Cell("vehicle", 864, 1384, 0), Cell("ldo", 300, 180, 0)]
    res = pack(cells)
    assert res.oversize == ["vehicle"]
    p, x, y = res.placements["vehicle"]
    assert p != res.placements["ldo"][0]
    # 左上锚:块顶边贴带顶,超高部分如实向带下溢出
    assert x == BAND[0]
    assert y == BAND[3] - 1384


def test_wide_short_block_not_oversize_with_keepout():
    # PACK-3(v0.6.11):扁宽块 1000×100 明明放得进图签上方全宽条带(1110×615),
    # 旧判据 w>bw-gap 误判 oversize 独占整页;修正后同页正常装箱
    cells = [Cell("bar", 1000, 100), Cell("ldo", 300, 180, 0)]
    res = pack(cells)
    assert res.oversize == []
    assert res.pages == 1
    # band 0 的 ldo 先放(带主序),bar 换行落左条带区,两块同页不重叠
    assert res.placements["bar"][0] == res.placements["ldo"][0]


def test_first_row_starts_at_band_top_left():
    # 2026-08-25 用户定案:每页第一块从带左上角起排(阅读序,自顶向下),
    # 不再自底向上把首行落在页脚。2026-08-31 间隙再收紧:piece=60,shelf=按行高 25%
    cells = [Cell("m0", 400, 250), Cell("m1", 400, 250), Cell("m2", 400, 250)]
    res = pack(cells)
    # 首块:左边缘贴带左,顶边贴带顶 → y = y2 - h
    assert res.placements["m0"][1:] == (BAND[0], BAND[3] - 250)
    # 第二块同行右侧(piece gap 60);第三块换行后顶边 = 首行底 - shelf_gap(250)
    assert res.placements["m1"][1:] == (BAND[0] + 400 + 60, BAND[3] - 250)
    assert res.placements["m2"][1:] == (BAND[0], BAND[3] - 250 - _shelf_gap(250) - 250)


def test_shelf_gap_adaptive_by_row_height():
    # 行间隙随行高:矮行贴下限 60,高行 25% 封顶 120(480+ 高行 → 顶)
    assert _shelf_gap(150) == 60
    assert _shelf_gap(250) == 62  # 62.5 → banker's round 62
    assert _shelf_gap(565) == 120
    assert _shelf_gap(800) == 120  # 封顶


def test_band_affinity_clusters_same_page():
    # 同带块优先同页:带 0 三小块 + 带 2 三小块,应该各自聚拢(允许 2 页内完成)
    cells = [Cell(f"pwr{i}", 200, 150, 0) for i in range(3)]
    cells += [Cell(f"peri{i}", 200, 150, 2) for i in range(3)]
    res = pack(cells)
    assert res.pages <= 2
    pwr_pages = {res.placements[f"pwr{i}"][0] for i in range(3)}
    peri_pages = {res.placements[f"peri{i}"][0] for i in range(3)}
    assert len(pwr_pages) == 1 and len(peri_pages) == 1, f"带未聚拢: {pwr_pages} {peri_pages}"


def test_group_affinity_same_page_and_observability():
    # module 组亲和(soft):mcu+卫星 与 motor+卫星 各自聚拢同页;affinity 记录
    # 每组实际落页,溢页可观测(本例两组都单页,note 无溢页项)
    cells = [
        Cell("mcu", 500, 300, 1, group="mcu"), Cell("swd", 150, 100, 1, group="mcu"),
        Cell("drv", 500, 300, 1, group="motor"), Cell("conn", 150, 100, 1, group="motor"),
    ]
    res = pack(cells)
    assert res.placements["swd"][0] == res.placements["mcu"][0]
    assert res.placements["conn"][0] == res.placements["drv"][0]
    assert res.affinity == {"mcu": [res.placements["mcu"][0]], "motor": [res.placements["drv"][0]]}
    assert "亲和溢页" not in res.note


def test_group_spill_recorded_when_page_full():
    # 组装不下一页时如实溢页:mcu 组 3 块 500×300,同排 2 块即满
    # (第 3 块换行压图签 keepout → 新页);motor 组 2 块落另一新页。
    # affinity 记 mcu 落 2 页,note 标"亲和溢页"
    cells = [Cell(f"mcu{i}", 500, 300, 1, group="mcu") for i in range(3)]
    cells += [Cell(f"mot{i}", 500, 300, 1, group="motor") for i in range(2)]
    res = pack(cells)
    mcu_pages = {res.placements[f"mcu{i}"][0] for i in range(3)}
    assert len(mcu_pages) == 2, f"mcu 应溢页: {mcu_pages}"
    assert len(res.affinity["mcu"]) == 2
    assert "亲和溢页" in res.note


def test_explicit_keepout_wraps_to_avoid():
    # 自定义 keepout(旧 keepout 区域)下 tall 块行尾候选压区 → 换行让位;
    # 间隙按 v0.6.11 公式(300 高行 → shelf gap 105)
    cells = [Cell("wide", 700, 300), Cell("tall", 200, 300)]
    res = pack(cells, keepout=(760, 660, 1100, 780))
    assert res.pages == 1
    assert res.placements["wide"][1:] == (BAND[0], BAND[3] - 300)
    assert res.placements["tall"][1:] == (BAND[0], BAND[3] - 300 - _shelf_gap(300) - 300)


def test_fewer_or_equal_pages_than_flow_baseline():
    # 流式对照:compile _PageFlow 单列宽块独行时页数 = 块数;装箱必须不差于它
    cells = [Cell(f"m{i}", 400, 300) for i in range(4)]
    res = pack(cells)
    # 4 块 400×300,带宽 1110:每行 2 块,两行高 300+105+300=705 ≤ 带高 765 → 1 页
    assert res.pages <= 2


def test_placements_complete_and_deterministic():
    cells = [Cell("a", 300, 200, 1), Cell("b", 250, 180, 2)]
    r1 = pack(cells)
    r2 = pack(cells)
    assert r1.placements == r2.placements
    assert set(r1.placements) == {"a", "b"}


def test_waste_reported_per_normal_page():
    cells = [Cell("solo", 200, 150)]
    res = pack(cells)
    assert len(res.waste) == 1 and res.waste[0] > 0.5  # 单小块单页,大半空白


def test_full_width_row_forces_next_page_with_keepout():
    # 1110 全宽块独占一行(顶条带 y595-795 不压图签);615 高的第二块换行后
    # 795-200-shelf_gap(200)-615 < 带底 → 如实开新页;两块都不算 oversize
    # (bar 恰好贴满带宽=整行独占,h≤615 放得进顶条带)
    cells = [Cell("bar", BAND_W, 200), Cell("tall", 300, 615)]
    res = pack(cells)
    assert res.oversize == []
    assert res.pages == 2
    assert res.placements["bar"][1:] == (BAND[0], BAND[3] - 200)
    assert res.placements["tall"][1:] == (BAND[0], BAND[3] - 615)
    assert TITLEBLOCK_KEEPOUT is not None  # 默认让位保持开启(2026-08-28 定案)
