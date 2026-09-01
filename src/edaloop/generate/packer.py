"""离线装箱分页(repack 阶段 2)。

输入是"已定框"的块 cell(来源:upstream 块=真机试放回读的实测 box 并集——
volume 口径,含 netport 文字/netflag/自有桩线;place 块=compile `_PLACE_INK`
估算),输出每块 (页号, x, y)。页内硬保证不重叠(货架数学),页数近似最少
(FFD),同带+同 module 块聚拢同页(亲和,soft——组装不下一页时如实溢页并记录)。

为什么不在真机上排:真机 arrange 每轮 3-6 次往返且受上游 wedge 影响;矩形装箱
是纯几何问题,离线可证。真机只保留终检(gate)与兜底(closeout)。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# A4 可用带:与 gate 验收权威(sch clusters 的 sheetUsable,真机实测 [12,12,1158,813])
# 同口径再留边距——不是 compile 的锚点安全带 [100,1100]×[300,780](那个只有 480 高,
# 会把 500 高的 mcu 类块误判 oversize)。装箱带对齐 gate,错就错在可验收的同一把尺上。
BAND = (30, 30, 1140, 795)
# 图签/明细表 keepout(视觉右下角;画布 y 向上,右下 = x 大 y 小)。2026-08-25
# 曾定案"图签从工程删除、装箱填满整带";2026-08-28 用户反转:**以后布局默认
# 按有图签**——建页默认带 A4 图签(Drawing-Symbol_A4),装箱让位右下角。
# 坐标按连接器 fitter 实测自洽标定(run-3ece9f39e1f2 P7 拒放回执"上方净高 615、
# 左侧净宽 456":795-615=180=图签顶边 y,486-30=456=图签左边 x;P4 实证
# 侵入件 x≈715 y≈120-180 触发、y≥645 的件不触发——y 区间在低处,非高处)。
TITLEBLOCK_KEEPOUT = (486.0, 30.0, 1140.0, 180.0)
# 间隙(v0.6.11 volume 口径化;2026-08-31 再收紧):body 口径时代 piece/shelf 双 200,
# 理由是相邻块的同名 netport 文字(宽 100-200)落在间隙里、clusters 把文字归错块
# (req-07 P3/P4/P5 实证)。volume 口径下 netport 文字/netflag/桩线已计入各自块体积,
# 间隙里只剩空画布,200→100 仍是双重计费(2026-08-31 freeze-pack 实测全页利用率
# 8-58%,"空白率太高"目检主诉)——收紧为 piece 60、shelf 按行高 25% 自适应(60-120)。
# 若真机 clusters 再现归属歧义(req-07 模式:layout-lint 过、clusters fail、组 box
# 互膨胀),回弹这两个常数即可,其余逻辑不动。
_PIECE_GAP = 60
_SHELF_GAP_MIN = 60
_SHELF_GAP_MAX = 120


def _shelf_gap(row_dy: float) -> float:
    """行间隙随行高:矮行 60,高行(≥240)→120。行越高越可能跨行走线,留的目检/绕行余量越大。"""
    return max(_SHELF_GAP_MIN, min(_SHELF_GAP_MAX, round(row_dy * 0.25)))


@dataclass
class Cell:
    name: str
    w: float
    h: float
    band_id: int = 1  # 0 电源 / 1 主控 / 2 外设(与 compile.band_of 同口径)
    kind: str = "upstream"  # upstream=实测框 / place=估算框
    group: str = ""  # 功能模块名(planner module 字段;空=未分组,带内排在同带分组块之后)


@dataclass
class PackResult:
    placements: dict[str, tuple[int, float, float]] = field(default_factory=dict)  # name -> (page_no 从 0 起, x, y)
    pages: int = 0
    oversize: list[str] = field(default_factory=list)
    waste: list[float] = field(default_factory=list)  # 每页空白率 0..1(仅普通页)
    affinity: dict[str, list[int]] = field(default_factory=dict)  # group -> 实际落页列表(升序去重;>1 页=亲和溢出)
    note: str = ""


def _hits_keepout(x: float, y: float, w: float, h: float, k: tuple[float, ...]) -> bool:
    """块矩形与 keepout 严格内部相交(擦边放过)。"""
    kx1, ky1, kx2, ky2 = k
    return x < kx2 and x + w > kx1 and y < ky2 and y + h > ky1


class _ShelfPage:
    """一页的货架状态:x 游标 + 当前行上沿。块按行铺,行宽尽换行,页高尽即页满。

    自顶向下铺(2026-08-25 用户定案:画布 y 向上,行从带顶 y2 往下叠,每页
    第一块锚在带左上角,阅读序——此前自底向上首行落在页脚,目检像"没从头排")。
    try_take 只在成功时推进游标——候选位置(含 keepout 让位换行)全部检查
    通过才提交,失败路径零状态污染。groups 记录页上出现过的 module 名,
    供 pack 的同组同页偏好用。
    """

    def __init__(self, band: tuple[float, ...], keepout: tuple[float, ...] | None) -> None:
        self.x1, self.y1, self.x2, self.y2 = band
        self.keepout = keepout
        self.row_top = self.y2  # 当前行上沿,自顶向下推进
        self.row_x = self.x1
        self.row_dy = 0.0
        self.area = 0.0
        self.groups: set[str] = set()

    def _candidate(self, w: float, h: float, wrap: bool) -> tuple[float, float] | None:
        """算候选 (x,y=块底边):wrap=True 先换行;页高尽/keepout 让不开 → None(不动状态)。

        换行间隙取"刚完成行"的行高(row_dy 此时未复位)——行是逐块垫高的,
        下一行上沿 = 当前行上沿 - 当前行高 - shelf_gap(行高)。
        """
        x, top = self.row_x, self.row_top
        if wrap:
            x, top = self.x1, self.row_top - self.row_dy - _shelf_gap(self.row_dy)
        y = top - h
        if x + w > self.x2 or y < self.y1:
            return None
        if self.keepout and _hits_keepout(x, y, w, h, self.keepout):
            return None
        return x, y

    def try_take(self, c: Cell) -> tuple[float, float] | None:
        """first-fit:当前行尾优先;撞 keepout 或行宽尽 → 换行重试;都不行 → None。"""
        for wrap in (False, True):
            pos = self._candidate(c.w, c.h, wrap)
            if pos is None:
                continue
            x, y = pos
            if wrap:
                # 提交换行:新行上沿 = 本块上沿,行状态复位
                self.row_top = y + c.h
                self.row_x = self.x1
                self.row_dy = 0.0
            self.row_x = x + c.w + _PIECE_GAP
            self.row_dy = max(self.row_dy, c.h)
            self.area += c.w * c.h
            if c.group:
                self.groups.add(c.group)
            return x, y
        return None


def pack(cells: list[Cell], band: tuple[float, ...] = BAND,
         keepout: tuple[float, ...] | None = TITLEBLOCK_KEEPOUT) -> PackResult:
    """FFD 货架装箱:带主序 + module 组序 + 面积降序 → first-fit 进已开页,页满开新页。

    keepout 默认 TITLEBLOCK_KEEPOUT(2026-08-28 用户定案:布局默认按有图签,
    右下角让位;显式传 None 恢复填满整带)。oversize 判定:v0.6.11 修正(PACK-3)
    ——设 keepout 时只看 L 形两条条带(上方全宽 top strip / 左侧全高 left strip)
    能否容纳,**不再叠加整带减间隙的预判**(旧判据 w>bw-gap 把 1000×100 这类
    "扁宽块"误判 oversize——它明明放得进图签上方全宽条带);keepout=None 时退回
    纯带测试(w>bw or h>bh,擦带满宽仍可整行独占)。oversize 块独占一页锚带放置
    并记入结果——REPLAN 信号前置到交付 review,不再等真机 gate 两轮 HALT 才发现。

    亲和(soft):同 group 的块在排序上相邻,first-fit 时优先尝试已含该组的页;
    组装不进一页时如实溢页,affinity 记录每个组的实际落页,>1 页即可观测。
    """
    res = PackResult()
    if not cells:
        return res
    x1, y1, x2, y2 = band
    bw, bh = x2 - x1, y2 - y1

    def _oversized(c: Cell) -> bool:
        if keepout is None:
            return c.w > bw or c.h > bh
        _kx1, _ky1, _kx2, ky2 = keepout
        top_h = y2 - ky2  # 图签上方条带净高(y 向上,图签在低处)
        left_w = _kx1 - x1  # 图签左侧条带净宽
        fits_top = c.w <= bw and c.h <= top_h
        fits_left = c.w <= left_w and c.h <= bh
        return not (fits_top or fits_left)

    oversize = [c for c in cells if _oversized(c)]
    big = {c.name for c in oversize}
    normal = [c for c in cells if c.name not in big]
    # 组 rank 按输入序(计划序≈功能模块序)定,同带内同组相邻;未分组排在同带分组块之后
    group_rank: dict[str, int] = {}
    for c in normal:
        if c.group and c.group not in group_rank:
            group_rank[c.group] = len(group_rank)
    # 带主序(电源先,同带聚拢)+ 组序(同模块相邻)+ 面积降序(大块先,FFD 近似最优)
    normal.sort(key=lambda c: (c.band_id, group_rank.get(c.group, 1 << 30), -(c.w * c.h)))

    pages: list[_ShelfPage] = []
    for c in normal:
        hit = None
        # 同组同页守卫:先试已含该组的页(保持序),再试其余页;无组/无同组页即普通 first-fit
        ordered = pages
        if c.group:
            ordered = [p for p in pages if c.group in p.groups] + [p for p in pages if c.group not in p.groups]
        for pg in ordered:
            pos = pg.try_take(c)
            if pos is not None:
                hit = (pages.index(pg), pos[0], pos[1])
                break
        if hit is None:
            pg = _ShelfPage(band, keepout)
            pages.append(pg)
            pos = pg.try_take(c)
            if pos is None:
                # 非 oversize 判定边角(数值临界):强制收页,保 placements 完整
                pos = (pg.x1, pg.y2 - c.h)
                pg.row_x, pg.row_dy, pg.area = pg.x1 + c.w + _PIECE_GAP, c.h, pg.area + c.w * c.h
            hit = (len(pages) - 1, pos[0], pos[1])
        res.placements[c.name] = hit

    # oversize 块:每块独占一页,锚带左上角(2026-08-25 用户定案:不再居中——
    # 独占块居中目检就是"第一块放中间";溢出方向如实出带,占位页不参与 first-fit)
    for c in oversize:
        res.placements[c.name] = (len(pages), x1, y2 - c.h)
        res.oversize.append(c.name)
        pages.append(_ShelfPage(band, None))

    res.pages = len(pages)
    full = bw * bh
    res.waste = [round(1.0 - p.area / full, 3) for p in pages[: len(pages) - len(oversize)]] or [1.0]
    # 亲和观测:每组的实际落页(升序去重)
    grp_pages: dict[str, set[int]] = {}
    for c in cells:
        if c.group and c.name in res.placements:
            grp_pages.setdefault(c.group, set()).add(res.placements[c.name][0])
    res.affinity = {g: sorted(v) for g, v in grp_pages.items()}
    splits = [g for g, v in res.affinity.items() if len(v) > 1]
    notes = []
    if oversize:
        notes.append(f"oversize×{len(oversize)}(独占页左上锚,REPLAN 候选)")
    if splits:
        notes.append(f"亲和溢页: {','.join(splits)}")
    res.note = "; ".join(notes)
    return res
