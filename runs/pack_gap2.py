import json
from edaloop.generate import packer as _pk

dims = {k: tuple(v) for k, v in json.load(open('runs/p2cells.json')).items()}
cells = [_pk.Cell(n, *dims[n], 1, "upstream") for n in dims]
band = (_pk.BAND[2] - _pk.BAND[0]) * (_pk.BAND[3] - _pk.BAND[1])
base = ['mcu_u1','led_fault','uln_st1','uln_st2']
for gap in (200, 160, 120, 100, 80, 60):
    _pk._PIECE_GAP = gap
    _pk._SHELF_GAP = gap
    res = _pk.pack(list(cells))
    p0 = [n for n, (p, x, y) in res.placements.items() if p == 0]
    area0 = sum(dims[n][0] * dims[n][1] for n in p0)
    extra = [f"{n}({dims[n][0]:.0f}x{dims[n][1]:.0f})" for n in p0 if n not in base]
    print(f"gap={gap:>3}: 页0 {len(p0):>2} 块, 覆盖 {area0/band:5.1%}, 新进: {extra}")
