import json
from edaloop.generate import packer as _pk

dims = {k: tuple(v) for k, v in json.load(open('runs/p2cells.json')).items()}
cells = [_pk.Cell(n, *dims[n], 1, "upstream") for n in dims]
band = (_pk.BAND[2] - _pk.BAND[0]) * (_pk.BAND[3] - _pk.BAND[1])
for gap in (200, 150, 120, 100, 60):
    _pk._PIECE_GAP = gap
    res = _pk.pack(list(cells))
    p0 = [n for n, (p, x, y) in res.placements.items() if p == 0]
    area0 = sum(dims[n][0] * dims[n][1] for n in p0)
    names = ['mcu_u1','led_fault','uln_st1','uln_st2']
    extra = [n for n in p0 if n not in names]
    print(f"gap={gap:>3}: 页0 {len(p0):>2} 块, 覆盖 {area0/band:5.1%}, 新进小块: {extra}")
