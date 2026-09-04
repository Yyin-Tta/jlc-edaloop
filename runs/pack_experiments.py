import json
from edaloop.generate import packer as _pk

dims = {k: tuple(v) for k, v in json.load(open('runs/p2cells.json')).items()}
def mk(names):
    return [_pk.Cell(n, *dims[n], 1, "upstream") for n in names]

def report(tag, cells):
    res = _pk.pack(cells)
    p0 = [n for n, (p, x, y) in res.placements.items() if p == 0]
    area0 = sum(dims[n][0] * dims[n][1] for n in p0)
    band = (_pk.BAND[2] - _pk.BAND[0]) * (_pk.BAND[3] - _pk.BAND[1])
    print(f"--- {tag}: pages={res.pages} 页0={len(p0)}块 覆盖={area0/band:.1%} waste={res.waste[0]:.1%}")
    for n in sorted(p0): print(f"      {n} {dims[n][0]:.0f}x{dims[n][1]:.0f} @{res.placements[n][1]:.0f},{res.placements[n][2]:.0f}")
    off = [n for n, (p, _, _) in res.placements.items() if p != 0]
    if off: print(f"      挤到第2页+: {sorted(off)}")
    return res

all29 = sorted(dims, key=lambda n: (-(dims[n][0]*dims[n][1]), -dims[n][1], -dims[n][0]))
# A: 复现本轮选择(29块,面积降序 FFD)
report("A 全部29块·面积降序(=本轮)", mk(all29))
# B: 只装 现有4块 + 用户点名的3小块
report("B 4大块+hdr_m2/r_cc1/c_ldo_in", mk(['mcu_u1','led_fault','uln_st1','uln_st2','hdr_m2','r_cc1','c_ldo_in']))
# C: 小块优先(块数最大化方向)
small_first = sorted(dims, key=lambda n: (dims[n][0]*dims[n][1], dims[n][1], dims[n][0]))
report("C 全部29块·面积升序(小块优先)", mk(small_first))
# D: 高度升序(矮行堆叠方向)
report("D 全部29块·高度升序", mk(sorted(dims, key=lambda n: (dims[n][1], dims[n][0]))))
