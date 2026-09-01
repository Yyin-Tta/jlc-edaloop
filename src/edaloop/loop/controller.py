from __future__ import annotations

import json
import math
import re
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from edaloop.generate.adapter import EasyedaAdapter
from edaloop.generate.audit import AuditLog
from edaloop.generate.compile import CLAIM_ZONE, compile_actions
from edaloop.generate.models import BlockPlan
from edaloop.generate.plan import ensure_std_candidates, make_plan
from edaloop.intent.ir import DesignIR
from edaloop.knowledge.models import BlockRecord
from edaloop.loop.attribution import attribute
from edaloop.validate.checks import validate
from edaloop.validate.models import Finding

MAX_ROUNDS = 5
SAME_CODE_HALT = 2


def _snap5(raw: float) -> int:
    """snap-5 且远离零取整(就近取整可差 1~2 单位仍越界/仍叠)。"""
    if raw == 0:
        return 0
    n = int(math.ceil(abs(raw) / 5.0) * 5)
    return n if raw > 0 else -n


def _clamp_delta(lo, hi, band_lo: float, band_hi: float) -> int:
    """把 [lo,hi] 钳回 [band_lo,band_hi] 的位移(0=已在带内)。"""
    if lo is None or hi is None:
        return 0
    raw = 0.0
    if hi > band_hi:
        raw = band_hi - hi
    elif lo < band_lo:
        raw = band_lo - lo
    return _snap5(raw) if raw else 0


def _leg_hits_rect(x1: float, y1: float, x2: float, y2: float, rect: tuple[float, ...], pad: float = 2.0) -> bool:
    """线段(任意角度)与矩形(四边各膨胀 pad)相交(slab 参数裁剪)。
    直线候选允许斜段后必须按一般线段-矩形求交,不能只查水平/垂直。"""
    rx1, ry1, rx2, ry2 = rect[0] - pad, rect[1] - pad, rect[2] + pad, rect[3] + pad
    dx, dy = x2 - x1, y2 - y1
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x1 - rx1), (dx, rx2 - x1), (-dy, y1 - ry1), (dy, ry2 - y1)):
        if p == 0:
            if q < 0:
                return False
        else:
            r = q / p
            if p < 0:
                if r > t1:
                    return False
                t0 = max(t0, r)
            else:
                if r < t0:
                    return False
                t1 = min(t1, r)
    return True


def _leg_near_point(x1: float, y1: float, x2: float, y2: float, px: float, py: float, tol: float = 3.0) -> bool:
    """线段(任意角度)是否贴近一个点:点到线段的最近距离 ≤ tol(端点外
    沿自动按端点距离算,等价于原轴对齐版的两端 ±tol 延伸)。"""
    dx, dy = x2 - x1, y2 - y1
    l2 = dx * dx + dy * dy
    if l2 == 0:
        return (px - x1) ** 2 + (py - y1) ** 2 <= tol * tol
    t = ((px - x1) * dx + (py - y1) * dy) / l2
    t = max(0.0, min(1.0, t))
    cx, cy = x1 + t * dx, y1 + t * dy
    return (px - cx) ** 2 + (py - cy) ** 2 <= tol * tol


_INTERNAL_NET_RE = re.compile(r"^[A-Z]+\d+_N\d+$")  # 块编译器内部网名(如 C16_N3)


def _legs_collinear_overlap(a1, b1, a2, b2, tol: float = 2.0) -> bool:
    """两条线段(任意角度)是否平行共线且跨度重叠(相接/交叠都算)——
    EasyEDA 会把这样的导线自动并成一条,并线即跨网短路(sch wire help
    明示的合并坑)。轴对齐是特例;直线候选允许斜段后需一般化。"""
    d1x, d1y = b1[0] - a1[0], b1[1] - a1[1]
    d2x, d2y = b2[0] - a2[0], b2[1] - a2[1]
    l1 = math.hypot(d1x, d1y)
    l2 = math.hypot(d2x, d2y)
    if l1 == 0 or l2 == 0:
        return False
    # 平行(方向叉积≈0,按较短边归一)
    if abs(d1x * d2y - d1y * d2x) > 0.02 * l1 * l2:
        return False
    # 共线:第二条的端点到第一条所在直线距离 ≤ tol
    if abs((a2[0] - a1[0]) * d1y - (a2[1] - a1[1]) * d1x) / l1 > tol:
        return False
    # 沿第一条方向投影重叠
    lo1, hi1 = 0.0, l1
    p2a = ((a2[0] - a1[0]) * d1x + (a2[1] - a1[1]) * d1y) / l1
    p2b = ((b2[0] - a1[0]) * d1x + (b2[1] - a1[1]) * d1y) / l1
    lo2, hi2 = min(p2a, p2b), max(p2a, p2b)
    return lo1 - tol < hi2 and lo2 - tol < hi1


def _legs_parallel_stack(a1, b1, a2, b2, sep: float = 25.0, min_overlap: float = 40.0) -> bool:
    """两条平行线段是否"近距堆叠":垂直间距在 (2, sep] 且沿线投影重叠
    ≥ min_overlap。共线(间距≤2)情形归 _legs_collinear_overlap 管,这里
    显式放过。真机 run-7bb0a226ac7d C16(J3 USB-C)目检定案:三条 875 长
    横线只隔 10(y=5265/5275/5285)、两条 285 长竖线只隔 5(x=830/835),
    缩放下糊成一团——"叠在一起非常影响观看"。引脚间距本身只有 10,
    贴脚收敛段(重叠<40)不算堆叠;只挡长平行段。v0.6.11 审计 P3:sep
    20→25,20 档实测仍显挤(目检 875 长线隔 20 与隔 10 观感接近)。"""
    d1x, d1y = b1[0] - a1[0], b1[1] - a1[1]
    d2x, d2y = b2[0] - a2[0], b2[1] - a2[1]
    l1 = math.hypot(d1x, d1y)
    l2 = math.hypot(d2x, d2y)
    if l1 == 0 or l2 == 0:
        return False
    if abs(d1x * d2y - d1y * d2x) > 0.02 * l1 * l2:
        return False  # 不平行
    dist = abs((a2[0] - a1[0]) * d1y - (a2[1] - a1[1]) * d1x) / l1
    if dist <= 2.0 or dist > sep:
        return False  # 共线归并线判据管;够远不算堆叠
    p2a = ((a2[0] - a1[0]) * d1x + (a2[1] - a1[1]) * d1y) / l1
    p2b = ((b2[0] - a1[0]) * d1x + (b2[1] - a1[1]) * d1y) / l1
    lo2, hi2 = min(p2a, p2b), max(p2a, p2b)
    return min(l1, hi2) - max(0.0, lo2) >= min_overlap


def _seg_point_dist(x1: float, y1: float, x2: float, y2: float, px: float, py: float) -> float:
    """点到线段距离(_leg_near_point 的标量版,择优净空算分用)。"""
    dx, dy = x2 - x1, y2 - y1
    l2 = dx * dx + dy * dy
    if l2 == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / l2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _route_pin_pair(p1, p2, foreign_bodies, foreign_pins, foreign_marks, wire_legs=()):
    """同网两脚的直连折线:返回去重后的点列,布不通返回 None。

    候选全集 = 直线(仅同行/同列——**平台 sch_PrimitiveWire.create 拒绝
    一切斜线**,真机 2026-08-25 实证 45°/任意斜线一律 "create failed!",
    横竖线皆可)→ L×2 → Z 角点阶梯(中点/贴目的地/贴起点 × 40/80)→
    U 源侧±40..±240 → U 目的地侧±40..±240(纯轴对齐,拐点吸附 10 单位网格)。
    **最短可行**(2026-08-26 定案):全候选评估后按 (总长, 拐点数, 生成序)
    择优——first-feasible 会在「源侧远档已通」时错过「目的地侧近档」,
    等长时生成序保旧偏好(直线>L>Z>U、源侧先于目的地侧、近档先于远档)。
    v0.6.11 审计 P3 在 key 里加**净空次级项**:等长等拐点时取离他网脚/
    标记更远的走线(贴 3 与隔 30 目观天差,旧版按生成序随机取近贴线)。
    U=绕行出列/出行再回——同行或同列两脚时 L/Z 全部退化成同一条直线,
    若行上恰有他网脚/标记(led 块 R↔LED 直列,LED 另一脚+GND 地标正落在
    行上,直线=真短路),只有 U 能避开。U 档位 40..240 步进 40:40/80 两档
    在引脚列密集区不够逃生(run-7bb0a226ac7d J3 目检:三条 875 长横线
    只隔 10,正是近档全灭后只能贴行的现场);v0.6.11 审计 P3 补 200/240
    远档(逃出整片密集引脚列)。约束:
    - 任一段不穿他件本体渲染框(外扩 2)、不压他网脚(容差 3,条目可为
      (x,y,tol) 覆写——同块近旁脚 1.5:贴行经过自家块内另一脚 2-3 单位
      无触点不算短路,压上才算)、不压他网标记**矩形**(v0.6.11:标记
      条目从锚点容差 5 改矩形——netport 文字宽 60-200,锚点判据只挡住
      文字起点,线从文字中间穿过照样压墨迹;矩形=锚点按 _mark_span 翼展
      展开,或调用方给实测 bbox)——压到脚=电气合并短路,穿本体/压文字
      =跨件并线+目观糊;
    - 不与他网既有线段(桩线/本轮已画直连线)共线重叠——并线即短路;
      也不近距堆叠(平行间距≤25 且重叠≥40)——长平行段贴在一起缩放下
      糊成一团(run-7bb0a226ac7d J3 目检:10 间距三层 875 长横线)。
    本网自己的桩/标记不在障碍内:先拆后画(disconnect 先行),画线时它们
    已不存在——真机 run-9e1c0a4e08d3 教训:先画后拆必被 EasyEDA 的"共端点
    即并线"合并成折返多段线,--flag-id 只查多段线端点,永远找不到桩。
    """

    def _snap(v: float) -> float:
        return round(v / 10.0) * 10.0

    x1, y1 = p1
    x2, y2 = p2
    cands = []
    if (x1 == x2 or y1 == y2) and (x1 != x2 or y1 != y2):
        cands.append([[x1, y1], [x2, y2]])
    cands += [
        [[x1, y1], [x2, y1], [x2, y2]],            # L 先横后竖
        [[x1, y1], [x1, y2], [x2, y2]],            # L 先竖后横
    ]
    # Z 角点阶梯(横竖横的竖边 x / 竖横竖的横边 y):中点先试,再贴目的地
    # (长段留在源脚行、到点前才拐,走廊互让),再贴起点,各 40/80 两档;
    # 必须严格落在两脚之间,否则退化/越界丢弃。只给中点时长横竖段只能
    # 贴脚行,走廊让不出来(run-7bb0a226ac7d J3 目检)。
    sx = 1.0 if x2 >= x1 else -1.0
    sy = 1.0 if y2 >= y1 else -1.0
    lo_x, hi_x = min(x1, x2), max(x1, x2)
    lo_y, hi_y = min(y1, y2), max(y1, y2)
    xm_seen: set = set()
    for xm in (_snap((x1 + x2) / 2.0), _snap(x2 - 40.0 * sx), _snap(x1 + 40.0 * sx),
               _snap(x2 - 80.0 * sx), _snap(x1 + 80.0 * sx)):
        if lo_x < xm < hi_x and xm not in xm_seen:
            xm_seen.add(xm)
            cands.append([[x1, y1], [xm, y1], [xm, y2], [x2, y2]])  # Z 横竖横
    ym_seen: set = set()
    for ym in (_snap((y1 + y2) / 2.0), _snap(y2 - 40.0 * sy), _snap(y1 + 40.0 * sy),
               _snap(y2 - 80.0 * sy), _snap(y1 + 80.0 * sy)):
        if lo_y < ym < hi_y and ym not in ym_seen:
            ym_seen.add(ym)
            cands.append([[x1, y1], [x1, ym], [x2, ym], [x2, y2]])  # Z 竖横竖
    for d in (40.0, 80.0, 120.0, 160.0, 200.0, 240.0):
        for yv in (_snap(y1 - d), _snap(y1 + d)):
            cands.append([[x1, y1], [x1, yv], [x2, yv], [x2, y2]])   # U 源侧出列再回
        for xv in (_snap(x1 - d), _snap(x1 + d)):
            cands.append([[x1, y1], [xv, y1], [xv, y2], [x2, y2]])   # U 源侧出行再回
    # U 目的地侧:源侧全被堵时的通道——典型是目的脚所在列被相邻引脚占满,
    # 只能沿脚行进场,而出通道在目的脚另一侧(真机 C16_N6 R8.1→J3.A5:
    # 源侧 U 的末段竖线全被 x=1100 引脚列挡死,目的地侧 -40 出到顶通道
    # y=5225 才通)。最短可行下源侧远档不再遮蔽目的地侧近档。
    for d in (40.0, 80.0, 120.0, 160.0, 200.0, 240.0):
        for yv in (_snap(y2 - d), _snap(y2 + d)):
            cands.append([[x1, y1], [x1, yv], [x2, yv], [x2, y2]])   # U 目的地侧出列再回
        for xv in (_snap(x2 - d), _snap(x2 + d)):
            cands.append([[x1, y1], [xv, y1], [xv, y2], [x2, y2]])   # U 目的地侧出行再回

    def _feasible(rt: list) -> tuple[list, float, float] | None:
        """去重 + 全约束检查;可行返回 (点列, 总长, 最小净空)。"""
        pts = [pt for i, pt in enumerate(rt) if i == 0 or pt != rt[i - 1]]
        if len(pts) < 2:
            return None
        legs = list(zip(pts, pts[1:]))
        length = 0.0
        clearance = 1e9  # 离最近他网脚/标记的净空(择优次级项)
        for a, b in legs:
            length += math.hypot(b[0] - a[0], b[1] - a[1])
            ok = True
            for rect in foreign_bodies:
                if _leg_hits_rect(a[0], a[1], b[0], b[1], rect):
                    ok = False
                    break
            if ok:
                for ent in foreign_pins:
                    tol = ent[2] if len(ent) > 2 else 3.0
                    if _leg_near_point(a[0], a[1], b[0], b[1], ent[0], ent[1], tol):
                        ok = False
                        break
                    clearance = min(clearance, _seg_point_dist(
                        a[0], a[1], b[0], b[1], ent[0], ent[1]))
            if ok:
                for rect in foreign_marks:
                    if _leg_hits_rect(a[0], a[1], b[0], b[1], rect):
                        ok = False
                        break
                    if clearance < 250.0:  # 矩形角点近似净空(够用:只做同分排序)
                        clearance = min(
                            clearance,
                            min(_seg_point_dist(a[0], a[1], b[0], b[1], cx, cy)
                                for cx, cy in ((rect[0], rect[1]), (rect[2], rect[1]),
                                               (rect[0], rect[3]), (rect[2], rect[3]))))
            if ok:
                for w2 in wire_legs:
                    if (_legs_collinear_overlap(a, b, w2[:2], w2[2:])
                            or _legs_parallel_stack(a, b, w2[:2], w2[2:])):
                        ok = False
                        break
            if not ok:
                return None
        return pts, length, clearance

    best_pts = None
    best_key: tuple | None = None
    for idx, rt in enumerate(cands):
        hit = _feasible(rt)
        if hit is None:
            continue
        pts, length, clearance = hit
        # 净空封顶 250:更远不再加分(避免"绕远换净空"反超长度主序)
        key = (round(length, 1), len(pts), -min(clearance, 250.0), idx)
        if best_key is None or key < best_key:
            best_pts, best_key = pts, key
    return best_pts


def _mst_edges(points: list[tuple[float, float]]) -> list[tuple[int, int]]:
    """曼哈顿距离 Prim 最小生成树(索引边,加入序)。多脚网直连的脚间
    配对此前按 (x,y) 排序链两两相连——排序序≠邻接序,折返拓扑上产生
    zig-zag 绕线(C16_N4 四脚网实证);MST 总长是任何链方案的下界。
    同距候选按 (x,y,索引) 定序,确定性输出。"""
    n = len(points)
    if n < 2:
        return []
    in_tree = [False] * n
    in_tree[0] = True
    dist = [abs(points[0][0] - points[j][0]) + abs(points[0][1] - points[j][1])
            for j in range(n)]
    src = [0] * n
    edges: list[tuple[int, int]] = []
    for _ in range(n - 1):
        j = min(
            (k for k in range(n) if not in_tree[k]),
            key=lambda k: (dist[k], points[k][0], points[k][1], k),
        )
        edges.append((src[j], j))
        in_tree[j] = True
        for k in range(n):
            if in_tree[k]:
                continue
            d = abs(points[j][0] - points[k][0]) + abs(points[j][1] - points[k][1])
            if d < dist[k]:
                dist[k] = d
                src[k] = j
    return edges


# 直连线长门(P0-1,2026-08-26 J3/J5 定案):块模板的 relational 布局按
# netport 标签连接设计,器距 500-900 是常态;无条件把标签跳线转成物理
# 直连线=横穿半图的长线(P1 J3:A5↔R11 曼哈顿 875 / P2 J5:A5↔R49 510)。
# 跨度(曼哈顿)超限不转,保留 netport 语义;对伴拉近失败的网同走此门。
_MAX_DIRECT_WIRE = 400.0
# 估算 cell 保守垫(v0.6.11 审计 P1):est 框(compile _PLACE_INK 级)不含
# netflag/netport 文字翼与桩线,实测 volume 通常比 est 大一截;试放失败退
# 估算的块按 cell=e+2*PAD、offsets=(-PAD,-PAD) 装——锚=装箱位+PAD,墨迹
# 四周各留 PAD,宁可页内虚一点,不让估算偏小把翼挤进邻格。
_EST_PAD = 120.0
# 对伴拉近(P1-6):只动 ≤4 脚小件(CC 下拉/串联对),落点距锚脚 ≤190,
# 每页上限 8 次(往返成本控制)。方向=大件中心→锚脚向外扇区优先。
_PULL_PIN_CAP = 4
_PULL_OFFSETS = (90.0, 140.0, 190.0)
_PULL_MAX = 8
# rail 孤件门:全脚皆 rail/空网(无内部网名可拉)的块内小件,离锚件最近脚
# 曼哈顿距离超此值才拉(ldo 块电容停在模板位 250-480 → 块摊成 oversize)。
_RAIL_PULL_GAP = 150.0


# clusters 报告缺 sheetUsable 时的回退带 = planner 契约带(plan.py 提示词与
# attribution.py 同源承诺 x∈[100,1100] y∈[300,780]);严格内嵌实测真带
# [12,12]-[1158,813] 且避图签带(y≥198)。2026-08-24 req-01 daily 实证:图框
# 几何丢失时上游 group-arrange 拒排 + 本地无带静默退 → 两轮零修复 HALT;
# 回退此带保住拒排兜底链(宽度足够 group-move 扫描落位,不依赖上游报告)。
_FALLBACK_SHEET_USABLE = (100.0, 300.0, 1100.0, 780.0)  # minX, minY, maxX, maxY


@dataclass
class RoundRecord:
    round_no: int
    plan_id: str = ""
    gate_verdict: str = "not-run"
    findings: list[Finding] = field(default_factory=list)
    feedback: str = ""
    halted: str = ""


class TrialFreezeSignal(RuntimeError):
    """EDALOOP_LAYOUT_FREEZE=1:试放+量框后冻结现场(已画虚线框+坐标/长宽标注),
    不装箱不重放不清页——轮次立即以 FREEZE 结束,页留给人工目检。"""


@dataclass
class LoopResult:
    status: str
    rounds: list[RoundRecord] = field(default_factory=list)
    final_plan: BlockPlan | None = None
    audit_dir: str = ""

    @property
    def converged_round(self) -> int | None:
        if self.status == "PASS":
            return len(self.rounds)
        return None


class LoopController:
    def __init__(
        self,
        ir: DesignIR,
        catalog: dict[str, BlockRecord],
        retrieve,
        llm,
        adapter: EasyedaAdapter,
        audit: AuditLog,
        *,
        max_rounds: int = MAX_ROUNDS,
        dry_run: bool = False,
        answer_context: str = "",
        retry_queries: list[str] | None = None,
        acceptance_items: list | None = None,
    ) -> None:
        self.ir = ir
        self.catalog = catalog
        self.retrieve = retrieve
        self.llm = llm
        self.adapter = adapter
        self.audit = audit
        self.max_rounds = max_rounds
        self.dry_run = dry_run
        self.answer_context = answer_context
        self.retry_queries = list(retry_queries or [])
        # P4-5①:验收条目(「## 期望指标」标注段,run 不再丢弃;空=无标注段)
        self.acceptance_items = list(acceptance_items or [])
        # P4-1② 功能分区编排(声明+整页分区框+分区注记),默认关——真机验证过再转默认开(风险 R17)
        self.zones_enabled = os.environ.get("EDALOOP_ZONES", "") in ("1", "true", "yes")
        # v0.6.11 审计 P1:生产模式画模块框(volume∪自画线),默认开,0/false/no 关
        self.frames_enabled = os.environ.get("EDALOOP_LAYOUT_FRAMES", "1").strip().lower() not in ("", "0", "false", "no")
        # 布局层弱告警(sch list 截断降载/钳移压叠降级/清页失败):轮内累积,
        # run() 复位并在 validate 后以 weak Finding 显性化——此前这类劣化
        # 只进审计,链路对"本轮为什么松"不可见(2026-08-26 P0/P2 批定案)。
        self._layout_warnings: list[dict] = []
        # P0 断网计数 / P1 自画线记账(v0.6.11 审计):run() 每轮复位;测试与
        # 分步调用路径不经过 run(),此处兜底初始化防 AttributeError
        self._wire_breaks: list[str] = []
        self._wire_boxes: dict[str, list[tuple[str, str, tuple[float, float, float, float]]]] = {}
        # P0 net 存在性终检结果(run() 每轮经 _net_presence 重算;此处兜底)
        self._net_missing: list[dict] = []
        # P0 收口前网基线(_apply 收口序开头 _snapshot_page_nets 写;终检第三通道)
        self._pre_closeout_nets: dict[str, set[str]] = {}

    def _cost_hint(self, candidates) -> str:
        """同功能可互换块的价格对比(实时查询,弱信号;仅 IR 有 cost_target 时生成,无诉求不查)。"""
        try:
            from edaloop.generate.bomcost import cost_hint_for_planner

            if not (self.ir.env and self.ir.env.cost_target):
                return ""
            groups: dict[str, list[dict]] = {}
            by_cat: dict[str, list] = {}
            for b in candidates:
                if b.lcsc:
                    by_cat.setdefault(b.category or "misc", []).append(b)
            interchangeable = {
                "interface": ("can", "rs485", "usb"),
                "power": ("ldo", "buck", "boost"),
            }
            for cat, keys in interchangeable.items():
                for key in keys:
                    parts = [
                        {"block_id": b.block_id, "lcsc": b.lcsc}
                        for b in by_cat.get(cat, [])
                        if key in b.block_id.lower() or key in b.name.lower()
                    ]
                    if len(parts) >= 2:
                        groups[f"{cat}:{key}"] = parts
            if not groups:
                return ""
            return cost_hint_for_planner(groups)
        except Exception:
            return ""

    def _augment_freeform(self, plan: BlockPlan, candidates, round_no: int) -> BlockPlan:
        """确定性拓扑模式增强:plan 中该功能仍是 uncovered 且模式原料在检索候选中时,
        用模式库分解结果替换(LLM 兜底失败时的可靠通道;LLM 已分解则不重复)。"""
        from edaloop.generate.freeform import decompose, match_pattern

        text = self.ir.query_text() + " " + (self.ir.source or "")
        pat = match_pattern(text)
        if pat is None:
            return plan
        prefix = pat["id"].split("-")[0]
        needed = {part["block_id"] for part in pat["parts"]}
        plan_ids = {b.block_id for b in plan.blocks}
        if needed <= plan_ids:
            return plan
        # 功能等价判重:LLM 已用 upstream 整块覆盖同功能时不高边注入
        # (如 up-pmos_highside_softstart 覆盖 highside-switch 模式)
        func_tokens = {
            "highside-switch": ("highside", "高边", "负载开关"),
            "reverse-polarity": ("reverse", "防反接", "xl1509", "vehicle_input"),
            "usb-esd": ("usblc", "esd"),
            "lowvolt-alarm": ("tl431", "lowbat", "alarm"),
        }
        func_words = func_tokens.get(pat["id"], ())
        if func_words and any(w in bid for bid in plan_ids for w in func_words):
            return plan
        cand_map = {b.block_id: b for b in candidates}
        blocks, notes = decompose(pat, cand_map, prefix)
        if not blocks:
            self.audit.event("freeform-miss", round_no=round_no, pattern=pat["id"], notes=notes)
            return plan
        plan.blocks.extend(blocks)
        plan.uncovered = [
            u for u in plan.uncovered if not any(k in u.lower() for k in pat["keywords"])
        ] + [f"[自由拓扑:{pat['id']}] {n}" if n else "" for n in notes]
        plan.uncovered = [u for u in plan.uncovered if u]
        self.audit.event("freeform-augment", round_no=round_no, pattern=pat["id"], added=[b.instance for b in blocks])
        return plan

    def run(self) -> LoopResult:
        result = LoopResult(status="FAIL", audit_dir=str(self.audit.dir))
        feedback = ""
        code_streak: dict[str, int] = {}
        if not self.dry_run:
            # 版本门前移(P5-0):此前只有 stage_apply 查版本,run 主链裸奔——
            # 真机 daily 在钉扎 0.25.1/实装 1.1.1 漂移下照常落图。真机首次
            # 变更前统一过门(ADR-0002);Fake/测试适配器无此方法则跳过。
            _check = getattr(self.adapter, "check_version", None)
            if callable(_check):
                _check()
        for round_no in range(1, self.max_rounds + 1):
            rec = RoundRecord(round_no=round_no)
            self._layout_warnings = []
            self._wire_breaks = []  # P0:紧凑化/重落的恢复失败计数(阈值门见 validate 段)
            self._wire_boxes = {}  # P1:各页自画直连线 bbox(画框口径=volume ∪ 自画线)
            self._net_missing = []  # P0:net 存在性终检结果(apply 后重算)
            self._pre_closeout_nets = {}  # P0:收口前网基线(终检第三通道,每轮重建)
            query = self.ir.query_text()
            digest = self.ir.decisions_digest()
            if digest:
                query = query + "\n" + digest
            candidates = list(self.retrieve(query))
            if self.retry_queries and round_no == 1:
                seen = {c.block_id for c in candidates}
                for rq in self.retry_queries:
                    for c in self.retrieve(rq):
                        if c.block_id not in seen:
                            candidates.append(c)
                            seen.add(c.block_id)
                self.audit.event("refine-retry", round_no=1, queries=self.retry_queries, candidates=len(candidates))
            # P4-4②:std R/C 通道常驻(提示词宣传的通道,检索没召回也要可用,否则目录外校验必杀)
            candidates = ensure_std_candidates(candidates, self.catalog)
            plan = make_plan(
                self.ir,
                candidates,
                self.llm,
                feedback=feedback,
                cost_hint=self._cost_hint(candidates),
                answer_context=self.answer_context,
            )
            plan = self._augment_freeform(plan, candidates, round_no)
            rec.plan_id = plan.id
            self.audit.event(
                "round-plan",
                round_no=round_no,
                plan_id=plan.id,
                blocks=[b.instance for b in plan.blocks],
                # 全量计划入审计(2026-08-31):此前只记块名,LLM 配额断供
                # (bigmodel 7 日上限)后想"不调 LLM 按原计划重跑落图"无从取
                # 计划——replay 只认原始动作事件,freeze-pack 审计没有。plan
                # 是纯 LLM 产物,不落盘就只能等配额。
                plan=json.loads(plan.model_dump_json()),
                uncovered=plan.uncovered,
                feedback=feedback,
            )
            gate_report = None
            apply_ok = True
            if not self.dry_run:
                # A4 标定(2026-08 真机):250 为实测可整块入图的格距;旧 600+150×(r-1)
                # 爬坡阶梯废弃——页流下放大 spacing 直接破页容量,重试走 per-block at/params.spacing
                actions = compile_actions(plan, self.catalog, spacing_default="250")
                self.adapter.clear_all_pages()
                # 两阶段布局(repack):试放定框→离线装箱→改写 at/page;失败自动
                # 回退流式(旧行为)。EDALOOP_LAYOUT=flow 一键关停。
                self._repack_oversize_pages: set[str] = set()
                if os.environ.get("EDALOOP_LAYOUT", "repack") == "repack":
                    try:
                        self._repack_actions(actions, plan, round_no)
                    except TrialFreezeSignal:
                        # 调试冻结:试放页已画框,跳过装箱/清页/重放/gate,立即收束
                        return LoopResult(status="FREEZE", audit_dir=str(self.audit.dir))
                pages = self._plan_pages(actions)
                # sch clear 只清各窗口当前活动页;上轮逐页 gate 会把前台留在末页,
                # 故每轮显式清文档全部既有页(含 P1 与超出本轮计划的孤儿页),
                # 否则 r≥2 叠上轮墨迹 → 文档级位号冲突(C8 类)确定性复发。
                existing = self._ensure_pages([p for p in pages if p != "P1"], round_no)
                # 清页保真(2026-08-21 决定性实验结论):sch clear --doc 本身不说谎
                # (连发六页全部真清空,remaining=0 如实),但其结果是三态——幸存时只往
                # result 塞 warning 仍 rc=0;且 r≥2 的清页紧跟上轮 apply,上游实证
                # 「block-apply 后立即 clear 可复现留 ~20 幸存者,数秒后手跑才能清空」。
                # rc 不可信:clear 后回读数器件才算数,幸存 → 重清一次(settle 电阻),
                # 两趟仍不清 → clear-fidelity 失败进审计(不静默;后续 apply 失败自会
                # 经 GATE_FAIL 归因,此处只负责把证据钉死)。
                clear_failed = [
                    p for p in self._page_order(existing | set(pages))
                    if not self._clear_page_verified(p, round_no)
                ]
                self.audit.event("page-clear", round_no=round_no, pages=pages, failures=clear_failed)
                if clear_failed:
                    # P0-3 门禁(2026-08-26):未清空的页上照常落图 = 残件+位号静默
                    # 改号+叠放的确定源(P1 184 件 freeze 残骸定性)。清页两趟失败
                    # 即跳过本轮落图,apply_ok=False 走既有 GATE_FAIL→RELAYOUT 反馈
                    # 重试路径;连败两轮由既有 code_streak→HALT 升级兜住。
                    self._layout_warnings.append({
                        "code": "PAGE_CLEAR_FAILED",
                        "evidence": f"清页两趟仍有残件:{','.join(clear_failed)};本轮跳过落图防叠残件",
                    })
                    apply_ok, gate_report = False, None
                else:
                    apply_ok, gate_report = self._apply(actions, round_no)
                    # P0 net 存在性终检:gate 判"接得合不合法",这里判"规划里的网
                    # 在不在页上"(req-07 P2 全页零 GND 形态 gate 漏报的补口)
                    self._net_missing = self._net_presence(actions, round_no)
                rec.gate_verdict = gate_report.get("verdict", "unknown") if gate_report else "not-run"
            # P4-4① sizing 轮内化:make_plan 后 validate 段计算(轨输入走 IR,出处随建议入审计),
            # PARAM_OFF_SPEC 弱观察与 feedback 注入都消费它;PASS 后 deliver 复用末轮结果。
            sizing_advices = self._size_round(plan, round_no)
            if round_no == 1 and self.acceptance_items:
                # P4-5①:验收条目进审计(标注段不再丢弃;复评结果随 round-validate 的 weak)
                self.audit.event(
                    "acceptance",
                    items=[
                        {"id": it.id, "source": it.source, "kind": it.kind, "check": it.check,
                         "checker": it.checker, "key": it.key}
                        for it in self.acceptance_items
                    ],
                )
            findings = validate(
                self.ir, plan, gate_report, catalog=self.catalog,
                sizing=sizing_advices or None, acceptance=self.acceptance_items or None,
                oversize_pages=getattr(self, "_repack_oversize_pages", None) or None,
            )
            self._last_acceptance_unmet = [f for f in findings if f.code == "ACCEPTANCE_UNMET"]
            # P0 断网计数门(审计):紧凑化/重落的恢复失败(拆桩后画线失败且回接
            # 失败/重落两档全败)不再是静默 fail-soft——≤3 处记弱告警,>3 处=
            # 系统性断网,以 error 阻断本轮(带断网继续=废板交付)。此前三处
            # 分支只写审计,req-07 类"整页 GND 消失"要靠人工翻审计才发现。
            wire_block: Finding | None = None
            if self._wire_breaks:
                if len(self._wire_breaks) > 3:
                    wire_block = Finding(
                        code="WIRE_RESTORE_BROKEN",
                        evidence=f"round {round_no}: 拆桩后画线失败且恢复失败 "
                                 f"{len(self._wire_breaks)} 处({'; '.join(self._wire_breaks[:5])});"
                                 f"继续落图=带断网交付,阻断本轮",
                        severity="error",
                        suggested_fix_class="RELAYOUT",
                    )
                else:
                    self._layout_warnings.append({
                        "code": "WIRE_RESTORE_WEAK",
                        "evidence": f"紧凑化/重落恢复失败 {len(self._wire_breaks)} 处:"
                                    f"{'; '.join(self._wire_breaks[:5])}(少量,交 gate 的 sch nets 兜底核对)",
                    })
            # 布局层弱告警显性化:validate 管电路对错,不管链路劣化——
            # 截断降载/压叠降级/清页失败只影响布局质量与证据可信度,weak 不挡轮。
            findings = findings + [
                Finding(
                    code=str(w.get("code", "LAYOUT_WARN")),
                    evidence=str(w.get("evidence", "")),
                    severity="warning",
                    suggested_fix_class=str(w.get("fix", "RELAYOUT")),
                    weak=True,
                )
                for w in self._layout_warnings
            ]
            if wire_block is not None:
                findings.append(wire_block)
            if self._net_missing:
                # P0:页上缺规划网=确定性断网(零载体),error 阻断——这是比 gate
                # 更硬的口径:网表里没有就是没有,没有"可能没问题"的余地
                for m in self._net_missing[:6]:
                    findings.append(Finding(
                        code="NET_MISSING",
                        evidence=f"round {round_no}: {m['page']} 页缺规划网 "
                                 f"{','.join(m['missing'][:8])}(页内零载体:线/桩/netport 皆无,断网)",
                        severity="error",
                        suggested_fix_class="REWIRE",
                    ))
            if not apply_ok:
                findings = [
                    Finding(
                        code="GATE_FAIL",
                        evidence=f"round {round_no}: block-apply 存在失败(autoconnect 连线失败或环境错误或清页失败跳过落图,详见 apply-error/page-clear 审计);本轮 spacing=250(A4 页流;RELAYOUT 反馈请给 at/params.spacing)",
                        severity="error",
                        suggested_fix_class="RELAYOUT",
                    )
                ] + findings
            rec.findings = findings
            blocking = [f for f in findings if not f.weak]
            self.audit.event(
                "round-validate",
                round_no=round_no,
                gate=rec.gate_verdict,
                blocking=[f.model_dump() for f in blocking],
                weak=[f.evidence for f in findings if f.weak],
                weak_codes=[f.code for f in findings if f.weak],  # P4-4④:与 weak 同序,refine 按码挑问题
            )
            result.rounds.append(rec)
            result.final_plan = plan
            if not blocking:
                result.status = "PASS"
                self.audit.event("loop-done", status="PASS", rounds=round_no)
                return result
            streak_key = "|".join(sorted({f.code for f in blocking}))
            code_streak[streak_key] = code_streak.get(streak_key, 0) + 1
            if code_streak[streak_key] >= SAME_CODE_HALT:
                result.status = "HALT"
                rec.halted = f"同错 {SAME_CODE_HALT} 轮:{streak_key},升级人工"
                self.audit.event("loop-halt", round_no=round_no, reason=rec.halted)
                return result
            feedback = attribute(blocking)
            # P4-4① sizing 输出经 feedback 注入下轮(值类建议带表内可用值提示,planner 采纳时
            # 用 resistor-std/capacitor-std 落图;只在还有下一轮时有意义,PASS 轮不经过此处)
            siz_fb = self._sizing_feedback(sizing_advices)
            if siz_fb:
                feedback = feedback + "\n" + siz_fb
            rec.feedback = feedback
        self.audit.event("loop-done", status="FAIL", rounds=self.max_rounds)
        return result

    def _size_round(self, plan, round_no: int) -> list:
        """P4-4①:本轮计划的确定性 sizing(轨输入走 IR;失败不阻断,只落审计)。"""
        try:
            from edaloop.generate.sizing import size_for_plan

            advices = size_for_plan(plan.blocks, ir=self.ir, catalog=self.catalog)
            self._last_sizing = advices
            self.audit.event(
                "sizing",
                round_no=round_no,
                advices=[
                    {
                        "kind": a.kind, "target": a.target, "rec": a.result_rec,
                        "rec_value": a.rec_value, "rec_kind": a.rec_kind, "nets": list(a.nets),
                        "inputs": [list(i) for i in a.inputs],
                    }
                    for a in advices
                ],
            )
            return advices
        except Exception as e:  # sizing 是弱增强:任何失败不拖垮主链路
            self._last_sizing = []
            self.audit.event("sizing-error", round_no=round_no, error=str(e)[:200])
            return []

    def _sizing_feedback(self, advices: list) -> str:
        """值类建议的 planner 反馈行(带标准件表命中状态;表内值才可直接落图)。"""
        try:
            from edaloop.generate.stdparts import lookup

            kind_map = {"resistance": "resistor", "capacitance": "capacitor"}
            lines = []
            for a in advices:
                if a.result_rec == "n/a":
                    continue
                if a.rec_value and a.rec_kind in kind_map:
                    hit = lookup(kind_map[a.rec_kind], a.rec_value)
                    tail = f"params.value={a.rec_value}" if hit else f"推荐 {a.rec_value}(就近换表内值)"
                    lines.append(f"- {a.kind}@{a.target}: {tail}")
                else:
                    lines.append(f"- {a.kind}@{a.target}: {a.result_rec}")
            if not lines:
                return ""
            return (
                "sizing 建议值(确定性公式,输入出处见 delivery.sizing.txt;采纳时用 "
                "resistor-std/capacitor-std 块 + params.value 表内标准值,值不要发明):\n" + "\n".join(lines[:10])
            )
        except Exception:
            return ""

    def deliver(self, result) -> dict:
        """PASS 后交付打包:SVG + 网表 + BOM 成本 + 摘要落 run 目录(§1 交付链路)。"""
        if result.status != "PASS" or self.dry_run:
            return {}
        import hashlib

        arts = {}
        try:
            # export-image 缺省只导前台页(末轮逐页 gate 把前台留在末页)——多页必须
            # 逐页 --doc 导出,否则交付物静默缺页(P1 电源页丢失类);单页保持原名。
            pages = sorted(
                {b.page or "P1" for b in (result.final_plan.blocks if result.final_plan else [])}
            )
            exported: list[str] = []
            for p in pages:
                name = "delivery.svg" if len(pages) == 1 else f"delivery-{p}.svg"
                svg_path = str((self.audit.dir / name).resolve())
                rc, _, _ = self.adapter.run(
                    ["sch", "export-image", "--out", svg_path, "--format", "svg", "--doc", p]
                )
                if rc == 0 and Path(svg_path).exists():
                    exported.append(svg_path)
            if exported:
                arts["svg"] = exported[0]
                arts["svg_pages"] = exported
        except Exception:
            pass
        try:
            rc, out, _ = self.adapter.run(["sch", "netlist"])
            if rc == 0:
                net = out
                (self.audit.dir / "delivery.net.json").write_text(net, encoding="utf-8")
                arts["netlist"] = str(self.audit.dir / "delivery.net.json")
                arts["netlist_sha256_16"] = hashlib.sha256(net.encode()).hexdigest()[:16]
        except Exception:
            pass
        try:
            from edaloop.generate.bomcost import summarize_bom

            placed: list[dict] = []
            for b in result.final_plan.blocks if result.final_plan else []:
                rec = self.catalog.get(b.block_id)
                part_refs = [
                    {"instance": f"{b.instance}:{p.ref}", "block_id": b.block_id, "lcsc": p.lcsc or ""}
                    for p in (rec.parts if rec else [])
                ]
                if part_refs:
                    placed.extend(part_refs)
                else:
                    placed.append(
                        {"instance": b.instance, "block_id": b.block_id, "lcsc": (rec.lcsc if rec else "") or ""}
                    )
            if placed:
                bom = summarize_bom(placed)
                try:
                    from edaloop.generate.selection import annotate_smt

                    lcscs = sorted({p["lcsc"] for p in placed if p.get("lcsc")})
                    smt = annotate_smt(lcscs)
                    for det in bom.get("details", []):
                        det["smt_type"] = smt.get(det.get("ref"), "unknown")
                    bom["smt_note"] = "库类型近似判定(JLC SMT API 无公开契约,R13 兜底):basic=基础库(免上料费倾向),extended=扩展库"
                except Exception as e:
                    self.audit.event("smt-annotate-error", error=str(e)[:150])
                (self.audit.dir / "delivery.bom.json").write_text(
                    json.dumps(bom, ensure_ascii=False, indent=1), encoding="utf-8"
                )
                arts["bom"] = str(self.audit.dir / "delivery.bom.json")
                arts["bom_total"] = bom.get("total")
        except Exception as e:
            self.audit.event("bom-cost-error", error=str(e)[:200])
        try:
            from edaloop.generate.selection import proposals_report, propose_swaps

            groups: dict[str, list[dict]] = {}
            for b in result.final_plan.blocks if result.final_plan else []:
                rec = self.catalog.get(b.block_id)
                if rec and rec.lcsc:
                    key = (rec.category or "misc").lower()
                    groups.setdefault(key, []).append({"block_id": b.block_id, "lcsc": rec.lcsc})
            groups = {k: v for k, v in groups.items() if len(v) >= 2}
            report = proposals_report(propose_swaps(groups)) if groups else "(无等价类组,跳过 swap 分析)"
            (self.audit.dir / "delivery.swap.txt").write_text(report, encoding="utf-8")
            arts["swap"] = str(self.audit.dir / "delivery.swap.txt")
        except Exception as e:
            self.audit.event("swap-error", error=str(e)[:200])
        try:
            # P4-4①:deliver 复用末轮轮内 sizing(缺则重算一次;输出口径含输入来源表)
            advices = getattr(self, "_last_sizing", None)
            if advices is None:
                from edaloop.generate.sizing import size_for_plan

                blocks_dicts = [
                    {"block_id": b.block_id, "instance": b.instance, "ports_binding": b.ports_binding}
                    for b in (result.final_plan.blocks if result.final_plan else [])
                ]
                advices = size_for_plan(blocks_dicts, ir=self.ir, catalog=self.catalog)
            if advices:
                (self.audit.dir / "delivery.sizing.txt").write_text(
                    "\n\n".join(a.render() for a in advices), encoding="utf-8"
                )
                arts["sizing"] = str(self.audit.dir / "delivery.sizing.txt")
                arts["sizing_count"] = len(advices)
        except Exception as e:
            self.audit.event("sizing-error", error=str(e)[:200])
        try:
            # P4-5①:验收清单交付(条目 + 末轮复评结果;manual 条目照列,人审)
            if self.acceptance_items:
                from edaloop.intent.acceptance import is_executable

                unmet = {f.where.ref for f in getattr(self, "_last_acceptance_unmet", [])}
                lines = []
                for it in self.acceptance_items:
                    mark = "✗ " if it.id in unmet else ("· " if is_executable(it.checker) else "? ")
                    lines.append(f"{mark}[{it.id}]({it.source}/{it.kind}) {it.check} → {it.checker}\n    期望: {it.expect}")
                for f in getattr(self, "_last_acceptance_unmet", []):
                    lines.append(f"  ✗ {f.evidence[:160]}")
                (self.audit.dir / "delivery.acceptance.txt").write_text(
                    "验收清单(✗=机械复评未满足 · =可执行已过 ? =manual 人审)\n" + "\n".join(lines),
                    encoding="utf-8",
                )
                arts["acceptance"] = str(self.audit.dir / "delivery.acceptance.txt")
        except Exception as e:
            self.audit.event("acceptance-error", error=str(e)[:200])
        try:
            from edaloop.loop.critic import render_report, review_plan

            if result.final_plan and result.final_plan.blocks:
                catalog_desc = {k: v.desc for k, v in self.catalog.items()}
                # P4-4④ 输入增强:网表摘要 + IR rails + sizing 建议值(critique 输入不再只有 plan 骨架)
                net_summary = ""
                try:
                    net_json = arts.get("netlist") and Path(arts["netlist"]).read_text(encoding="utf-8") or ""
                    if net_json:
                        net = json.loads(net_json)
                        nets = net.get("nets") or net.get("netlist") or []
                        names = [n.get("name", n.get("net", "")) if isinstance(n, dict) else str(n) for n in nets]
                        net_summary = f"{len(names)} nets: " + ", ".join(sorted(filter(None, names))[:60])
                except Exception:
                    net_summary = ""
                rails_summary = "; ".join(
                    f"{r.name}={r.v_text()}" + (f" imax={r.imax:g}A" if r.imax is not None else "")
                    for r in self.ir.power.rails
                )
                sizing_summary = "\n".join(
                    f"{a.kind}@{a.target}: {a.result_rec}" for a in (getattr(self, "_last_sizing", None) or []) if a.result_rec != "n/a"
                )
                findings = review_plan(
                    result.final_plan,
                    self.llm,
                    catalog_desc=catalog_desc,
                    netlist_summary=net_summary,
                    rails_summary=rails_summary,
                    sizing_summary=sizing_summary,
                )
                summary = f"{len(result.final_plan.blocks)} blocks, status={result.status}"
                (self.audit.dir / "delivery.review.txt").write_text(
                    render_report(findings, summary), encoding="utf-8"
                )
                arts["review"] = str(self.audit.dir / "delivery.review.txt")
                arts["review_findings"] = len(findings)
                self.audit.event("critic", findings=[f.model_dump() for f in findings])
        except Exception as e:
            self.audit.event("critic-error", error=str(e)[:200])
        self.audit.event("delivery", artifacts=arts)
        return arts

    def _verify_pins(self, round_no: int, designator: str, pinout: dict[str, str] | None, page: str = "P1") -> bool:
        """place 后回读符号 pin 集合,与库 pinout diff(三方校验的落地端);--page 定页读。"""
        if not pinout:
            return True
        try:
            from edaloop.generate.adapter import AdapterError  # 就近:except 引用必须本地可见(#5)
            read = self._run_json_retry(["sch", "read", "--page", page or "P1"])
        except AdapterError as e:
            self.audit.event("pin-verify", round_no=round_no, designator=designator, error=str(e)[:500])
            return True
        placed = next(
            (c for c in read.get("result", {}).get("components", []) if c.get("designator") == designator),
            None,
        )
        if not placed:
            self.audit.event("pin-verify", round_no=round_no, designator=designator, error="回读未找到器件")
            return False
        symbol_pins = {p.get("number"): p.get("name") for p in placed.get("pins", [])}
        diff = {
            k: (symbol_pins.get(k), pinout.get(k))
            for k in set(symbol_pins) | set(pinout)
            if symbol_pins.get(k) != pinout.get(k)
        }
        ok = not diff and len(symbol_pins) == len(pinout)
        self.audit.event(
            "pin-verify",
            round_no=round_no,
            designator=designator,
            symbol=len(symbol_pins),
            expected=len(pinout),
            ok=ok,
            diff={k: v for k, v in list(diff.items())[:10]},
        )
        return ok

    @staticmethod
    def _jitter_at(args: list[str], delta: int = 40) -> list[str]:
        """轮内重试时对 --at 坐标做确定性偏移(避开原冲突几何;A4 尺度下 40 ≈ 半格距)。"""
        try:
            i = args.index("--at")
            x, y = args[i + 1].split(",")
            args = list(args)
            args[i + 1] = f"{int(x) + delta},{int(y) + delta}"
        except (ValueError, IndexError):
            pass
        return args

    @staticmethod
    def _page_order(names) -> list[str]:
        """页名规范序:P1 恒首,其余 P<n> 按号升序,非规范名殿后。"""

        def order(p: str) -> tuple[int, int]:
            m = re.fullmatch(r"P(\d+)", p.strip())
            if p == "P1":
                return (0, 0)
            return (1, int(m.group(1))) if m else (2, 0)

        return sorted(set(names), key=order)

    @classmethod
    def _plan_pages(cls, actions) -> list[str]:
        """动作涉及的落图页(P1 恒首,其余按页号升序;无落图动作回退 P1)。

        块按高度重排后动作流首现序不保证 P1 打头(实测 P4 曾打头,调用方
        pages[1:] 误把 P4 当 P1 跳过 → 漏建页 → --doc 落图全炸);
        建页/清页/逐页 gate/复核统一消费本序。
        """
        raw = {a.page or "P1" for a in actions if a.kind in ("block-apply", "sch-place")}
        return cls._page_order(raw) or ["P1"]

    def _ensure_pages(self, want: list[str], round_no: int) -> set[str]:
        """P4-b2 多页提前量:compile 判定分页后,落图前按名建页(幂等,已存在跳过)。

        P5-0 增页修剪:eval 复用同一工程,page-clear 只清内容不删页 → 页数单调涨
        (实测 38 页后 EasyEDA netlist 导出超时"returned no file",block-apply 内置
        净验证全缺脚假阴性 → GATE_FAIL,2026-08-22 实证);先删计划外的 P\\d+ 孤儿页
        再建页。page-new 无名(上游 v0.25.1 无 --name),需 page-rename 两段;变更类
        命令单次执行不走重试通道(重试会双建页);失败只审计不判负——后续 --doc 落图
        命令会显式失败并走既有 apply-fatal 路径。
        返回修剪+建页后文档剩余页名(调用方据此逐页全清;非 harness 命名的页不动,
        仍走清内容路径)。
        """
        from edaloop.generate.adapter import AdapterError

        self._warmup()
        try:
            info = self._run_json_retry(["sch", "pages"])
        except AdapterError as e:
            self.audit.event("pages-read-error", round_no=round_no, error=str(e)[:500])
            return set()
        res = info.get("result", {}) or {}
        entries = list(res.get("pages") or [])
        if not entries:
            for s in res.get("schematics", []) or []:
                entries.extend(s.get("page", []) or [])
        have = {str(p.get("name", "")).strip() for p in entries if str(p.get("name", "")).strip()}
        sch_uuid = next((p.get("parentSchematicUuid") for p in entries if p.get("parentSchematicUuid")), "")
        # P5-0 页修剪:只删 harness 命名形态(^P\\d+$)且不在本轮计划的页;单发不重试
        # (变更类纪律,同 page-new);删失败不判负,残留页由逐页清内容路径兜底。
        keep = set(want) | {"P1"}
        by_name = {str(p.get("name", "")).strip(): p.get("uuid", "") for p in entries}
        doomed = sorted(n for n in have if re.match(r"^P\d+$", n) and n not in keep)
        deleted: list[str] = []
        prune_failed: list[str] = []
        for name in doomed:
            uuid = by_name.get(name, "")
            try:
                if not uuid:
                    raise AdapterError(f"sch pages 未返回该页 uuid: {name}")
                rc, _, _ = self.adapter.run(["sch", "page-delete", "--page", uuid])
                if rc == 0:
                    deleted.append(name)
                else:
                    prune_failed.append(name)
            except Exception as e:  # noqa: BLE001 - 删页失败不判负,残留页走清内容路径
                prune_failed.append(name)
                self.audit.event("page-prune-error", round_no=round_no, name=name, error=str(e)[:300])
        if doomed:
            self.audit.event("page-prune", round_no=round_no, deleted=deleted, failed=prune_failed)
            have -= set(deleted)
        for name in want:
            if name in have:
                continue
            try:
                if not sch_uuid:
                    raise AdapterError("sch pages 未返回 parentSchematicUuid,无法建页")
                r = self.adapter.run_json(["sch", "page-new", "--schematic", sch_uuid])
                inner = r.get("result", r) if isinstance(r, dict) else {}
                page_uuid = (inner or {}).get("pageUuid") or (inner or {}).get("uuid") or ""
                if not page_uuid:
                    raise AdapterError(f"page-new 未返回 uuid: {str(r)[:200]}")
                rc, _, _ = self.adapter.run(["sch", "page-rename", "--page", page_uuid, "--name", name])
                self.audit.event("page-create", round_no=round_no, name=name, uuid=page_uuid, rc=rc)
                if rc == 0:
                    have.add(name)  # 仅成功改名才计入(失败页留着由 --doc 落图显式暴露)
            except Exception as e:  # noqa: BLE001 - 建页失败不判负,交由后续 --doc 命令显式暴露
                self.audit.event("page-create-error", round_no=round_no, name=name, error=str(e)[:500])
        # P2 页序重建(v0.6.11 审计):`sch pages` 返回序=页签序,实测工程乱序
        # ([P3,P2,P4,…,P1]),CLI 无 page-reorder 命令——用户目检"模块随机放"
        # 的一半感知来自页签乱跳。乱序时幂等重建(见 _rebuild_page_order);
        # 页内容本轮必被 clear+重放,重建无损;任何失败只审计不判负。
        try:
            rebuilt = self._rebuild_page_order(round_no)
        except Exception as e:  # noqa: BLE001
            self.audit.event("page-reorder-error", round_no=round_no, error=str(e)[:300])
            rebuilt = None
        return rebuilt if rebuilt is not None else have

    def _rebuild_page_order(self, round_no: int) -> set[str] | None:
        """乱序 P 页幂等重建:锚页(首个非规范名页,无则首个 P 页改名
        __reorder_tmp__)保持文档非空+前台 → 删其余 ^P\\d+$ 页 → 按号升序
        page-new+rename → 删临时锚。返回重建后终态页名集;序已正/页数≤1/
        任何一步失败 → None(调用方保留原 have)。变更类命令单发不重试。"""
        from edaloop.generate.adapter import AdapterError

        try:
            info = self._run_json_retry(["sch", "pages"])
        except AdapterError:
            return None
        entries = list((info.get("result", {}) or {}).get("pages") or [])
        order: list[str] = []
        uuid_of: dict[str, str] = {}
        for p in entries:
            n = str(p.get("name", "")).strip()
            if n and n not in uuid_of:
                uuid_of[n] = str(p.get("uuid", ""))
                order.append(n)
        p_pages = [n for n in order if re.match(r"^P\d+$", n)]
        if len(p_pages) <= 1 or p_pages == self._page_order(p_pages):
            return None
        sch_uuid = next((p.get("parentSchematicUuid") for p in entries
                         if p.get("parentSchematicUuid")), "")
        if not sch_uuid:
            return None
        anchor = next((n for n in order if not re.match(r"^P\d+$", n)), None)
        tmp = "__reorder_tmp__"
        if anchor is None:
            anchor = tmp
            anchor_uuid = uuid_of[p_pages[0]]
            rc, _o, err = self.adapter.run(
                ["sch", "page-rename", "--page", anchor_uuid, "--name", tmp])
            if rc != 0:
                self.audit.event("page-reorder-abort", round_no=round_no,
                                 step="rename-anchor", error=(err or "")[:200])
                return None
            uuid_of[tmp] = anchor_uuid  # 后续 open 锚页/删锚都按 uuid 寻址
            victims = p_pages[1:]
        else:
            victims = p_pages
        # 锚页置前台:删活动页可能被拒,顺带把前台固定在不动页上
        self.adapter.run(["sch", "open", "--page", uuid_of[anchor]])
        for name in victims:
            rc, _o, _e = self.adapter.run(["sch", "page-delete", "--page", uuid_of[name]])
            if rc != 0:
                # 漏删的页与将建页同名冲突 → 中止重建(终态重读兜底),乱序留下轮;
                # 锚页若已是临时名,先改回原名回收(__reorder_tmp__ 永久残留无路径)
                if anchor == tmp:
                    self.adapter.run(["sch", "page-rename", "--page", uuid_of[tmp],
                                      "--name", p_pages[0]])
                self.audit.event("page-reorder-abort", round_no=round_no,
                                 step="delete", page=name)
                break
        else:
            created: list[str] = []
            for name in self._page_order(p_pages):
                try:
                    r = self.adapter.run_json(["sch", "page-new", "--schematic", sch_uuid])
                    inner = r.get("result", r) if isinstance(r, dict) else {}
                    page_uuid = (inner or {}).get("pageUuid") or (inner or {}).get("uuid") or ""
                    rc, _o, _e = self.adapter.run(
                        ["sch", "page-rename", "--page", page_uuid, "--name", name])
                    if rc == 0 and page_uuid:
                        created.append(name)
                except Exception as e:  # noqa: BLE001
                    self.audit.event("page-reorder-create-error", round_no=round_no,
                                     name=name, error=str(e)[:300])
            if anchor == tmp:
                self.adapter.run(["sch", "page-delete", "--page", uuid_of[tmp]])
            self.audit.event("page-reorder", round_no=round_no,
                             old=p_pages, created=created)
        try:
            info2 = self._run_json_retry(["sch", "pages"])
            return {str(p.get("name", "")).strip()
                    for p in (info2.get("result", {}) or {}).get("pages") or []
                    if str(p.get("name", "")).strip()}
        except AdapterError:
            return None

    def _gate_all_pages(self, gate_args: list[str], actions, round_no: int) -> dict:
        """逐页 gate:上游 gate 只校验活动页,多页必须 --doc 逐页跑;verdict 取最坏,stages 并集带页标。"""
        from edaloop.generate.adapter import AdapterError

        worst = {"pass": 0, "unknown": 1, "blocked": 1, "fail": 2}
        merged: dict = {"verdict": "pass", "stages": []}
        for p in self._plan_pages(actions):
            verdict_page = "unknown"
            stages_page: list = []
            try:
                rep = self._run_json_retry(list(gate_args) + ["--doc", p])
                verdict_page = rep.get("verdict", "unknown")
                stages_page = rep.get("stages", []) or []
            except AdapterError as e:
                self.audit.event("gate-error", round_no=round_no, page=p, error=str(e)[:500])
                verdict_page = "blocked"
            if worst.get(verdict_page, 2) > worst.get(merged["verdict"], 2):
                merged["verdict"] = verdict_page
            merged["stages"].extend([dict(s, page=p) for s in stages_page])
            self.audit.event(
                "gate",
                round_no=round_no,
                page=p,
                verdict=verdict_page,
                stages=[
                    f"{s.get('stage') or s.get('name')}:{s.get('verdict') or s.get('status')}"
                    for s in stages_page
                ],
            )
        return merged

    @staticmethod
    def _doc_args(act) -> list[str]:
        """页钉扎:一切带页的落图动作(含 P1)追加全局 --doc(上游无 --page,--doc 是唯一
        页选择器,CLI 自动切页并核对 document.current,refuse 而落错页)。

        P1 不豁免:--doc 切换是粘性的,前台可能停在上一动作切去的页——不带 --doc 的
        变更命令会落错页且与该页首块锚点(100,300)精确相撞(2026-08-21 req-02 真机
        三轮全灭的根因)。sch-gate 除外:_gate_all_pages 自行逐页钉扎。
        """
        if act.page and act.kind != "sch-gate":
            return act.args + ["--doc", act.page]
        return act.args

    def _apply_zone_frames(self, round_no: int, zone_pages: dict[str, dict[str, list[str]]], actions) -> None:
        """P4-1②/P4-b2 功能分区编排(逐页):zones clear → set(真实位号) → zone-plan(审计)
        → zone-draw → 分区注记。

        注释层操作,单次执行不走通用重试通道(note 重跑会产生重复注释);失败不判负
        (分区框是注释不是电气对象,按弱信号处理),全部入审计;zone-plan 五项校验计数
        留作 P4-4 门禁接线的数据源,本轮只记不拦。注记锚 (100+i*350, 230):避图签
        keepout(y≤198 且 x≥468)且各认领横向错开防 label 碰撞。
        """
        from edaloop.generate.adapter import AdapterError

        for page, claims in sorted(zone_pages.items()):
            doc = ["--doc", page]
            page_actions = [a for a in actions if (a.page or "P1") == page]
            try:
                rc, _, _ = self.adapter.run(["sch", "zones", "clear", *doc])
                self.audit.event("zones-clear", round_no=round_no, page=page, rc=rc)
                set_args = ["sch", "zones", "set"]
                for claim, desigs in sorted(claims.items()):
                    zone_vocab = CLAIM_ZONE.get(claim, ("center", claim))[0]
                    uniq = list(dict.fromkeys(d for d in desigs if d))
                    set_args += ["--module", f"{claim}={zone_vocab}:{','.join(uniq)}"]
                rc, out, _ = self.adapter.run(set_args + doc)
                self.audit.event(
                    "zones-set",
                    round_no=round_no,
                    page=page,
                    rc=rc,
                    claims={c: len(v) for c, v in claims.items()},
                    out=(out or "")[:300],
                )
                validation: dict = {}
                plan_ok = False
                try:
                    plan = self._run_json_retry(["sch", "zone-plan", "--json", *doc])
                    plan_ok = True
                    validation = plan.get("validation") or {}
                    self.audit.event(
                        "zone-plan",
                        round_no=round_no,
                        page=page,
                        validation=validation,
                        partitions=len(plan.get("partitions", []) or []),
                    )
                except AdapterError as e:
                    self.audit.event("zone-plan-error", round_no=round_no, page=page, error=str(e)[:500])
                # partitionOverlap 非 0 = 两区体积真互压(上游定论)→ zone-draw 必拒。
                # zone-arrange --apply 是上游专用解(断言①删除=重建 → 落位重连 →
                # 断言② 曾连 pin 仍连 → lint+bridge-check,任一红逐步回滚);重排后
                # 重 plan 再画,仍脏则 draw 照旧拒、归反馈域(弱信号不判负)。
                fixable = ("sheetOverflow", "partitionOverlap", "titleBlockHits", "sheetMarginHits")
                if plan_ok and any(validation.get(k) for k in fixable):
                    rc_za, out_za, err_za = self.adapter.run(["sch", "zone-arrange", "--apply", *doc])
                    self.audit.event(
                        "zone-arrange",
                        round_no=round_no,
                        page=page,
                        rc=rc_za,
                        out=(out_za or "")[-300:],
                        error=(err_za or "")[-300:],
                    )
                    try:
                        plan = self._run_json_retry(["sch", "zone-plan", "--json", *doc])
                        validation = plan.get("validation") or {}
                        self.audit.event(
                            "zone-plan",
                            round_no=round_no,
                            page=page,
                            validation=validation,
                            partitions=len(plan.get("partitions", []) or []),
                        )
                    except AdapterError as e:
                        self.audit.event("zone-plan-error", round_no=round_no, page=page, error=str(e)[:500])
                rc, _, _ = self.adapter.run(["sch", "zone-draw", "--mode", "partition", *doc])
                self.audit.event("zone-draw", round_no=round_no, page=page, rc=rc)
                for i, claim in enumerate(sorted(claims)):
                    label = CLAIM_ZONE.get(claim, ("", claim))[1]
                    names = [
                        a.desc.split(" @")[0].split("(")[0].strip()
                        for a in page_actions
                        if a.zone == claim and a.kind in ("block-apply", "sch-place")
                    ]
                    text = f"{label}: " + " / ".join(dict.fromkeys(names))
                    rc, _, _ = self.adapter.run(
                        ["sch", "note", "--text", text, "--x", str(100 + i * 350), "--y", "230", "--zone", claim, *doc]
                    )
                    self.audit.event("zone-note", round_no=round_no, page=page, claim=claim, rc=rc, text=text[:200])
            except AdapterError as e:
                self.audit.event("zones-fatal", round_no=round_no, page=page, error=str(e)[:1000])

    def _clusters_report(self, page: str) -> dict:
        """clusters --json 全量报告(带几何/簇 box/flags),解析失败返回空。

        rc!=0 是常态(ERROR 即非零),解析只认 stdout;不带 --strict:tight 是
        WARN,属 gap 参数域,不触发拆排。"""
        _, out, _ = self.adapter.run(["sch", "clusters", "--json", "--doc", page])
        try:
            return json.loads(out) if (out or "").strip() else {}
        except ValueError:
            return {}

    def _cluster_errors(self, page: str) -> list[dict]:
        rep = self._clusters_report(page)
        return [f for f in (rep.get("findings") or []) if f.get("level") == "ERROR"]

    def _arrange_closeout(
        self,
        round_no: int,
        placed_by_page: dict[str, dict[str, list[str]]],
        zone_by_page: dict[str, dict[str, list[str]]] | None = None,
    ) -> None:
        """P4-b3 布局收口(逐页,仅 clusters ERROR 页):拆问题块组 → 逐件单件组
        → group-arrange 刚体平移(网表逐 pin 不变)→ 复查,仍 ERROR 换大 gap 重排一次。

        为什么拆到单件:block-apply 封组让整块成为刚体,块内碰撞(usbc J1
        netport 翼展压 D3,校准 A 实测)任何整块平移都修不掉;单件化后
        arrange 按耦合逐件落位(usbc 结构死局实证 2 ERROR→0)。干净块保持
        整组刚体、干净页整页不动(重排会把已达标几何重洗,收益为负)。
        副作用已知并接受:snap-5 平移可产生 marker 微叠(大 gap 重排兜底);
        移动内核清扫不重建 NC 标(floating=warn 弱门禁;no-connect 有引入
        真短路的实锤,绝不自动补)。全程非致命:收口失败只审计,gate 是最终权威。"""
        from edaloop.generate.adapter import AdapterError

        for page, insts in sorted(placed_by_page.items()):
            try:
                errs = self._cluster_errors(page)
                if not errs:
                    continue
                self.audit.event(
                    "arrange-probe",
                    round_no=round_no,
                    page=page,
                    errors=[{"type": f.get("type"), "a": f.get("a"), "b": f.get("b")} for f in errs],
                )
                # ERROR 位号点名谁拆谁;对不上号(位号漂移/上轮残留)→ 组全拆
                err_desigs = {d for f in errs for d in (f.get("a"), f.get("b")) if d}
                # 分流(v0.6.11 审计 P2):先最小干预——无组点名件补单件组+钳回
                # 带,清零即返。整页 group-arrange 是收口重洗:装箱布局本已无重叠
                # 验证,ERROR 只可能是局部越带/微叠,刚移点名件就够;旧路径无差别
                # 重排整页,装箱阅读序毁于收口(用户"模块随机放"目检的另一半来源)。
                self._group_strays(round_no, page, insts, err_desigs)
                errs = self._clamp_into_band(round_no, page, (zone_by_page or {}).get(page))
                if not errs:
                    self.audit.event("arrange-result", round_no=round_no, page=page,
                                     remaining=0, path="clamp-only")
                    continue
                self._shatter_groups(round_no, page, insts, err_desigs)
                # gap 梯子自适应(run5/run6 实证):执行过仍脏(rc=0)是微叠类 → 放大;
                # 拒排 rc=1 是装不下类(run6 实测 P2 总需仅超带 4~24 单位)→ 逐档缩小
                # 60→40。仍救不回(P1/P6 类:arrange 的组占地含挂线,拆成单件也不缩
                # 翼展——run6 现场实验 7 单件仍拒)→ 钳回兜底刚移点名件
                gap, tried = 80, []
                for _ in range(3):
                    rc, out, err = self.adapter.run(
                        ["sch", "group-arrange", "--annotate=false", "--gap", str(gap), "--doc", page]
                    )
                    tried.append(gap)
                    self.audit.event(
                        "arrange-apply",
                        round_no=round_no,
                        page=page,
                        gap=gap,
                        rc=rc,
                        out=(out or "")[-300:],
                        error=(err or "")[-300:],
                    )
                    errs = self._cluster_errors(page)
                    if not errs:
                        break
                    if rc == 0:
                        nxt = 140 if 140 not in tried else None
                    else:
                        nxt = next((g for g in (60, 40) if g not in tried), None)
                    if nxt is None:
                        break
                    gap = nxt
                if errs:
                    errs = self._clamp_into_band(round_no, page, (zone_by_page or {}).get(page))
                self.audit.event("arrange-result", round_no=round_no, page=page, remaining=len(errs))
            except AdapterError as e:
                self.audit.event("arrange-fatal", round_no=round_no, page=page, error=str(e)[:800])

    def _clamp_into_band(
        self, round_no: int, page: str, zone_map: dict[str, list[str]] | None = None
    ) -> list[dict]:
        """拒排兜底:按 clusters 可用带把 ERROR 件刚移到空位。

        为什么这条路成立:gate/clusters 的 sheetUsable 含图签带(实测
        [12,12]-[1158,813],801 高),比 arrange 的排布带([12,198] 起,615 高)
        宽——收口的验收是 clusters 零 ERROR,不是过 arrange;arrange 拒排时
        它什么都没动,点名件还在带外,刚移(group-move 挂线跟随)即可。

        一次一动:每步后重探再决策(实测 P6 三件连钳,J4 落在 J3 上、R10 压
        R9——钳回带内 ≠ 钳到空位)。落点沿钳回轴向带内扫 60 步进避邻居,
        有 zone 认领时优先落本 zone 包络旁(run7 残留 partitionOverlap=3 实证
        裸钳会把件甩进邻居分区);overlap 双方都在带内 → b 沿 y 下推/上推 40
        分离。(位号,dx,dy) 发过即入 spent 不再重发——拒过的重发=4 次空转
        (run8 P1/P2 实证),成功过的重发=几何绕圈回原状的振荡(req-05 P3 J1
        实证 -180/+180/-180 循环)。无解就停,归反馈域。"""
        zone_map = zone_map or {}
        claim_of = {d: claim for claim, ds in zone_map.items() for d in ds}
        spent: set[tuple[str, int, int]] = set()  # 拒过的+已发过的(位号,dx,dy),都不再发
        # 预算随首轮 ERROR 数伸缩:固定 4 次「一次一动」在 7 件越界页必剩 3 件
        # (req-05 P1 实锤:探针 7 件、钳 4 件、remaining=3,r2 整页重排同形 →
        # 同码连胜 HALT);×2 余量吸收移动牵出的新 ERROR。退出条件不变——清零
        # 即返/无可动即返,预算纯防呆上限,不影响收敛判定。
        errs = self._cluster_errors(page)
        if not errs:
            return []
        for _ in range(max(4, 2 * len(errs))):
            rep = self._clusters_report(page)
            errs = [f for f in (rep.get("findings") or []) if f.get("level") == "ERROR"]
            if not errs:
                return []
            u = rep.get("sheetUsable") or {}
            try:
                ux1, uy1, ux2, uy2 = (float(u[k]) for k in ("minX", "minY", "maxX", "maxY"))
            except (KeyError, TypeError, ValueError):
                # 旧:带几何缺失静默返回(无证据不动作)。clusters 无带 → 回退
                # planner 契约带并留痕,ERROR 件仍可刚移——拒排兜底链不断。
                self.audit.event(
                    "clamp-band-fallback",
                    round_no=round_no,
                    page=page,
                    missing=str(u)[:120],
                )
                ux1, uy1, ux2, uy2 = _FALLBACK_SHEET_USABLE
            boxes = {
                c.get("designator"): c.get("box")
                for c in (rep.get("clusters") or [])
                if c.get("designator")
            }
            # 位号 → 所在组(shatter 后点名件应为单件组;组=刚移单位,挂线自动跟随)
            _, out, _ = self.adapter.run(["sch", "group", "list", "--json", "--doc", page])
            try:
                grp_rep = json.loads(out) if (out or "").strip() else {}
                groups = [g for gs in (grp_rep.get("groupsByPage") or {}).values() for g in gs]
            except ValueError:
                groups = []
            gid_of: dict[str, str] = {}
            for g in groups:
                gid = g.get("id") or g.get("name") or ""
                for m in g.get("members") or []:
                    d = m.get("designator")
                    if d and gid and d not in gid_of:
                        gid_of[d] = gid
            acted = False

            def _zone_bbox(finding: dict) -> tuple[float, float, float, float] | None:
                """点名件同 zone 其他成员的联合包络(自身除外;无箱或独居 → None)。"""
                d = finding.get("a") or ""
                members = zone_map.get(claim_of.get(d, ""), [])
                bb = [boxes[m] for m in members if m != d and boxes.get(m)]
                if not bb:
                    return None
                return (
                    min(x["minX"] for x in bb),
                    min(x["minY"] for x in bb),
                    max(x["maxX"] for x in bb),
                    max(x["maxY"] for x in bb),
                )

            for f in errs:
                cands = self._clamp_moves_for(f, boxes, gid_of, ux1, uy1, ux2, uy2, _zone_bbox(f))
                move = next((c for c in cands if (c[0], c[2], c[3]) not in spent), None)
                if not move:
                    if not cands:
                        # 无候选诊断(req-05 P2 U4 实锤:探针点名却永不动作,离线无从
                        # 知道是缺箱/缺组/带内无空位)——把判定依据钉进审计再谈修复
                        d = f.get("a") or ""
                        b = boxes.get(d) or {}
                        self.audit.event(
                            "clamp-no-candidate",
                            round_no=round_no,
                            page=page,
                            type=f.get("type"),
                            designator=d,
                            has_box=bool(b),
                            in_group=d in gid_of,
                            box=str(b)[:120],
                        )
                    continue
                d, gid, dx, dy = move
                rc, _, err = self.adapter.run(
                    ["sch", "group-move", "--group", gid, "--dx", str(dx), "--dy", str(dy), "--doc", page]
                )
                # 已发过的(成功与否)不再发:成功位移重推导=几何绕了一圈回到原状
                # (req-05 P3 J1 实锤:-180/+180/-180 循环),spent 断环路
                spent.add((d, dx, dy))
                acted = True
                self.audit.event(
                    "arrange-clamp",
                    round_no=round_no,
                    page=page,
                    cause=f.get("type"),
                    designator=d,
                    group=gid,
                    dx=dx,
                    dy=dy,
                    rc=rc,
                    error=(err or "")[-200:],
                )
                break  # 一次一动,下一步用新鲜几何
            if not acted:
                return errs
        rep = self._clusters_report(page)
        return [f for f in (rep.get("findings") or []) if f.get("level") == "ERROR"]

    def _clamp_moves_for(
        self,
        finding: dict,
        boxes: dict[str, dict],
        gid_of: dict[str, str],
        ux1: float,
        uy1: float,
        ux2: float,
        uy2: float,
        zone_bbox: tuple[float, float, float, float] | None = None,
    ) -> list[tuple[str, str, int, int]]:
        """一条 ERROR → 候选刚移序列 [(位号,组id,dx,dy)] 按偏好序;无解 []。

        内核才是落点权威(run8 P1/P2 实证:clusters 带含图签条,按带查可行
        的下推目标仍可撞图签 keepout 被钳成 Δ0;连线树共享邻件 pin 则整组
        拒移、与方向无关),所以这里交全序候选,调用方逐个试、拒过的不再发。
        out-of-sheet:钳回带内,落点沿移动轴向内扫 60 步进避邻居,zone 认领
        时按离包络中心最近排序(无认领保持最小位移优先);
        overlap:先 b 后 a,各先下推 40 再上推(下推可行性按新 minY 整箱
        查——旧版查 maxY 会放过半出带的目标,keepout 拒移即源于此)。"""
        margin = 15.0

        def occupied(d: str, b: dict) -> bool:
            return any(
                d != o
                and b["minX"] - margin < ob.get("maxX", 1e9)
                and b["maxX"] + margin > ob.get("minX", -1e9)
                and b["minY"] - margin < ob.get("maxY", 1e9)
                and b["maxY"] + margin > ob.get("minY", -1e9)
                for o, ob in boxes.items()
            )

        if finding.get("type") == "out-of-sheet":
            d = finding.get("a")
            b = boxes.get(d or "")
            if not b or d not in gid_of:
                return []
            dx = _clamp_delta(b.get("minX"), b.get("maxX"), ux1, ux2)
            dy = _clamp_delta(b.get("minY"), b.get("maxY"), uy1, uy2)
            if dx == dy == 0:
                return []
            # 2D 网格扫描(req-05 P2 实锤:轴锁死扫 6 步全被占,±120 横偏仍不够
            # 大件落位):主轴=越界轴(双出界取量大者),钳回后同向 60 步进×8
            # 深入带内;横轴兜底 0/±60…±360(半幅带)。落点逐个过带界检查,
            # 有空位只进空位;候选序=位移最小优先,zone 认领时再按包络中心重排。
            primary_x = abs(dx) >= abs(dy)

            def _ladder(delta: int) -> list[int]:
                if delta:
                    s = -60 if delta < 0 else 60
                    return [delta + i * s for i in range(8)]
                return [0]

            def _cross() -> list[int]:
                return [x for m in range(7) for x in ((m * 60), (-m * 60))][1:]

            in_band: list[tuple[tuple[int, int], dict]] = []
            for m in _ladder(dx if primary_x else dy):
                for c in _cross():
                    cand = (m, c + dy) if primary_x else (c + dx, m)
                    tb = {
                        "minX": b["minX"] + cand[0],
                        "maxX": b["maxX"] + cand[0],
                        "minY": b["minY"] + cand[1],
                        "maxY": b["maxY"] + cand[1],
                    }
                    if (
                        tb["minX"] >= ux1
                        and tb["maxX"] <= ux2
                        and tb["minY"] >= uy1
                        and tb["maxY"] <= uy2
                    ):
                        in_band.append((cand, tb))
            free = [(cand, tb) for cand, tb in in_band if not occupied(d, tb)]
            if not free:
                # 带内无空位兜底(req-05 P2 U4 实锤:239×412 簇箱在带内被整页
                # 簇箱铺满,±360×8 级扫描仍零空位):改选「带内+压叠数最少」落点,
                # 把 out-of-sheet 降级成 overlap——交给下一轮探针的 overlap 钳
                # 沿 y 分移;b 侧 40 步分离 + spent 断环,不会无限拉锯。
                # P2-9(2026-08-26):降级不再无声——压叠最少≠不叠,weak 告警
                # 随轮末显性化,持续出现即页容量不足的 REPLAN 证据。
                w = {
                    "code": "CLAMP_OVERLAP_DOWNGRADE",
                    "evidence": (
                        f"{finding.get('a')} 带内无空位,钳回降级为压叠最少落点"
                        f"(out-of-sheet→overlap,下一轮探针再分离;持续出现=页容量不足)"
                    ),
                }
                if w not in self._layout_warnings:
                    self._layout_warnings.append(w)
                def _n_overlaps(bb: dict) -> int:
                    return sum(
                        1
                        for o, ob in boxes.items()
                        if d != o
                        and bb["minX"] - margin < ob.get("maxX", 1e9)
                        and bb["maxX"] + margin > ob.get("minX", -1e9)
                        and bb["minY"] - margin < ob.get("maxY", 1e9)
                        and bb["maxY"] + margin > ob.get("minY", -1e9)
                    )

                ranked = sorted(
                    in_band,
                    key=lambda ct: (_n_overlaps(ct[1]), abs(ct[0][0]) + abs(ct[0][1])),
                )[:8]
                if not ranked:
                    return []
                return [(d, gid_of[d], c[0], c[1]) for c, _ in ranked]
            if zone_bbox:
                zx = (zone_bbox[0] + zone_bbox[2]) / 2
                zy = (zone_bbox[1] + zone_bbox[3]) / 2
                free.sort(
                    key=lambda ct: ((ct[1]["minX"] + ct[1]["maxX"]) / 2 - zx) ** 2
                    + ((ct[1]["minY"] + ct[1]["maxY"]) / 2 - zy) ** 2
                )
            return [(d, gid_of[d], c[0], c[1]) for c, _ in free]
        if finding.get("type") == "overlap":
            a, b_ = finding.get("a"), finding.get("b")
            ba, bb = boxes.get(a or ""), boxes.get(b_ or "")
            if not ba or not bb:
                return []
            moves: list[tuple[str, str, int, int]] = []
            for p, q in ((b_, a), (a, b_)):  # 先动 b;b 不可动再动 a
                if p not in gid_of:
                    continue
                bp, bq = boxes[p], boxes[q]
                down = _snap5(bq["minY"] - 40 - bp["maxY"])  # p 下移到 q 下方 40
                if down and bp["minY"] + down >= uy1:
                    moves.append((p, gid_of[p], 0, down))
                up = _snap5(bq["maxY"] + 40 - bp["minY"])  # p 上移到 q 上方 40
                if up and bp["maxY"] + up <= uy2:
                    moves.append((p, gid_of[p], 0, up))
            return list(dict.fromkeys(moves))  # 同(位号,dx,dy)去重保序
        return []

    def _shatter_groups(
        self, round_no: int, page: str, insts: dict[str, list[str]], err_desigs: set[str]
    ) -> None:
        """按 ERROR 点名拆组:问题件的封组解体 → 点名件单件化(arrange 获得逐件
        自由度),无辜件重新封组保持刚体——块作者标定几何少受扰动,参与 arrange
        的体积也小(run5 实证整块全拆=6-9 组在 y∈[198,813] 可用带装不下,
        点名拆能把排布体积压回可容纳)。err_desigs 对不上任何落图位号(位号
        漂移/上轮残留)→ 该页所有组全拆(拆多不拆错:漏拆=整块仍刚体,块内
        碰撞永远修不掉)。

        另:place 通道件不自动归组,不属于任何组的落图件补建单件组——
        否则 arrange 对它们零手段(out-of-sheet 永存,run5 P3/P4 实证)。"""
        _, out, _ = self.adapter.run(["sch", "group", "list", "--json", "--doc", page])
        try:
            rep = json.loads(out) if (out or "").strip() else {}
            groups = [g for gs in (rep.get("groupsByPage") or {}).values() for g in gs]
        except ValueError:
            groups = []
        all_placed = {d for ds in insts.values() for d in ds}
        targeted = bool(err_desigs & all_placed)
        covered: set[str] = set()
        for g in groups:
            gid = g.get("id") or g.get("name") or ""
            members = [m.get("designator", "") for m in (g.get("members") or []) if m.get("designator")]
            if not gid or not members:
                continue
            covered |= set(members)
            culprits = set(members) & err_desigs if targeted else set(members)
            if not culprits:
                continue  # 干净组:整组保持刚体
            rest = sorted(set(members) - culprits)
            if not rest and len(members) == 1:
                continue  # 已是点名单件组(strays 刚补建):拆了重建=同构换 id 空转
            self.adapter.run(["sch", "group", "ungroup", "--group", gid, "--doc", page])
            for d in sorted(culprits):
                self.adapter.run(["sch", "group", "create", "--members", d, "--doc", page])
            if len(rest) >= 2:
                self.adapter.run(["sch", "group", "create", "--members", ",".join(rest), "--doc", page])
            elif rest:
                self.adapter.run(["sch", "group", "create", "--members", rest[0], "--doc", page])
            self.audit.event(
                "arrange-shatter",
                round_no=round_no,
                page=page,
                group=gid,
                culprits=sorted(culprits),
                rest=rest,
            )
        for d in sorted(all_placed - covered):
            self.adapter.run(["sch", "group", "create", "--members", d, "--doc", page])
            self.audit.event("arrange-stray-group", round_no=round_no, page=page, designator=d)

    def _group_strays(self, round_no: int, page: str, insts, err_desigs: set[str]) -> None:
        """最小干预分组(v0.6.11 审计 P2):只把「无组」的 ERROR 点名件各自
        建单件组,已有组的一律不动(block 组=刚体,整组钳回带正是想要的语义;
        place 通道件落图无组,钳移的组单位缺失才补)。

        与 _shatter_groups 的区别:那边整页重组(收口重洗),这边只补缺失的
        移动单位——分流路径先走这里+_clamp_into_band,清零即返,装箱布局
        不被整页 group-arrange 重洗。"""
        all_placed = {d for ds in insts.values() for d in ds}
        _, out, _ = self.adapter.run(["sch", "group", "list", "--json", "--doc", page])
        try:
            grp_rep = json.loads(out) if (out or "").strip() else {}
            groups = [g for gs in (grp_rep.get("groupsByPage") or {}).values() for g in gs]
        except ValueError:
            groups = []
        covered: set[str] = set()
        for g in groups:
            covered |= {str(m.get("designator", "")) for m in (g.get("members") or [])
                        if m.get("designator")}
        for d in sorted((all_placed & err_desigs) - covered):
            self.adapter.run(["sch", "group", "create", "--members", d, "--doc", page])
            self.audit.event("arrange-stray-group", round_no=round_no, page=page, designator=d)

    def _apply_titleblocks(self, round_no: int, actions) -> None:
        """逐页明细表只读保位(注释类非致命)。

        上游 0.26.0 明令 titleblock --data 写入禁令:该写路径触发图签重建、
        损毁 sheet 符号引用 → 重启后图框丢失 → group-arrange 拒动 → overlap
        修不掉 → HALT(2026-08-24 req-01 daily 定案,时序 09:59 写/11:48 强杀
        重启/13:21 全灭)。标题文字的收益(一条注释)远低于该链路代价,不再由
        harness 写入;--show 只是可见性开关(非禁令写路径),失败只审计不判负。"""
        from edaloop.generate.adapter import AdapterError

        for page in self._plan_pages(actions):
            try:
                rc, out, err = self.adapter.run(["sch", "titleblock", "--show", "--doc", page])
                self.audit.event(
                    "titleblock",
                    round_no=round_no,
                    page=page,
                    show_rc=rc,
                    out=(out or "")[-200:],
                    error=(err or "")[-120:],
                )
            except AdapterError as e:
                self.audit.event("titleblock-error", round_no=round_no, page=page, error=str(e)[:500])

    def _apply(self, actions, round_no: int) -> tuple[bool, dict | None]:
        from edaloop.generate.adapter import AdapterError

        self._warmup()
        ok_all = True
        gate_report = None
        uuids: dict[str, tuple[str, str]] = {}
        failed: set[str] = set()
        place_pinouts: dict[str, dict[str, str]] = {}
        # 位号避撞(req-07 r2 实证):block-apply 子件由 EasyEDA 全项目顺序排号
        # (vehicle 24 件占 C1-C9/R1-R7/…),std 件再请求 C1 会触发**异步注解静默
        # 改号**(place 回执照抄请求名,活态已是 C15;网表 read 短窗内仍报旧名
        # → pin-verify 假过)→ autoconnect 按请求名找不到件 rc≠0。先到先得:
        # 本轮已引入位号登记在册,place 撞号换同前缀下一空号,并同步改写该件
        # 全部 autoconnect 引用。
        taken_desig: set[str] = set()
        renamed_desig: dict[str, str] = {}  # 请求名 → 实际落点名
        zone_designators: dict[str, dict[str, list[str]]] = {}  # P4-1②/P4-b2:页 → claim → 本轮落图位号
        placed_by_page: dict[str, dict[str, list[str]]] = {}  # P4-b3:页 → 实例 → 落图位号(拆组重排用)
        for act in actions:
            try:
                args = self._doc_args(act)  # P4-b2:非 P1 页追加 --doc 钉扎
                if act.kind == "sch-gate":
                    # P4-b3 收口次序(v0.6.11 对抗评审后与 freeze=pack 同序):
                    # 网基线快照 → 脚旋转 → 越带桩重落 → 拆组重排 → 逐页紧凑化
                    # → 分区框 → 明细表,全部先于 gate(zone-draw 按落图后几何画
                    # 框、明细表作用前台页,重排必须最先)。
                    # ①rotate/reseat 此前只在 freeze 跑:生产保留 U 形绕行/带外
                    #   桩,预演几何≠交付几何(closeout 被迫用钳移/拆排去修 reseat
                    #   本可消掉的 ERROR)。
                    # ②紧凑化在 closeout 之后(与 freeze 同序):模块框并入的自画
                    #   线 bbox 才是移动后的最终墨迹(颠倒则框罩旧位漏新位);
                    #   代价是重排时组框仍含 netport 文字(排得松些,gap 吸收)。
                    if not self.dry_run:
                        for pg in sorted(placed_by_page):
                            self._snapshot_page_nets(pg, round_no)
                            self._rotate_outward_pins(pg, round_no)
                            self._reseat_escape_marks(pg, round_no)
                        self._arrange_closeout(round_no, placed_by_page, zone_designators)
                        for pg in sorted(placed_by_page):
                            self._compact_internal_nets(pg, round_no, placed_by_page[pg])
                        # 紧凑化后复探(2026-08-31):compact 的拉近在 closeout 之后
                        # 仍移动器件,收口探针看不见末端几何;终态重叠/出纸在此
                        # 分离,分离后再补一轮压体/斜甩标记重落;随后末轮 reseat
                        # 后终态复探+收口(2026-09-01 布局治本批,同 freeze 收口
                        # 尾:「顺序即盲区」——reseat 的盲落/重落在探针后变异
                        # 几何,复探分离本体相交后再收口一轮,有界收敛)
                        for pg in sorted(placed_by_page):
                            self._overlap_reprobe(
                                pg, round_no, placed_by_page[pg],
                                oversize=pg in getattr(self, "_repack_oversize_pages", set()))
                            self._reseat_escape_marks(pg, round_no)
                            self._overlap_reprobe(
                                pg, round_no, placed_by_page[pg],
                                oversize=pg in getattr(self, "_repack_oversize_pages", set()))
                            self._reseat_escape_marks(pg, round_no)
                        if self.zones_enabled and zone_designators:
                            self._apply_zone_frames(round_no, zone_designators, actions)
                        self._apply_titleblocks(round_no, actions)
                        if self.frames_enabled:
                            self._draw_module_frames(round_no, actions, placed_by_page)
                    gate_report = self._gate_all_pages(act.args, actions, round_no)
                    continue
                if act.kind == "lib-search":
                    resp = self._run_json_retry(act.args)
                    lib, uuid = self._first_uuid(resp)
                    if not lib and act.mpn and act.mpn.upper() != act.lcsc.upper():
                        resp = self._run_json_retry(
                            ["lib", "search", "--query", act.mpn, "--limit", "3"]
                        )
                        lib, uuid = self._first_uuid(resp)
                        if lib:
                            self.audit.event(
                                "lib-search-fallback",
                                round_no=round_no,
                                instance=act.block_instance,
                                mpn=act.mpn,
                                lib=lib,
                                uuid=uuid,
                            )
                    if not lib:
                        ok_all = False
                        failed.add(act.block_instance)
                        self.audit.event(
                            "apply-fatal",
                            round_no=round_no,
                            instance=act.block_instance,
                            error=f"lib search 无结果: {act.lcsc}",
                        )
                        continue
                    uuids[act.block_instance] = (lib, uuid)
                    self.audit.event(
                        "lib-search", round_no=round_no, instance=act.block_instance, lib=lib, uuid=uuid
                    )
                    continue
                if act.block_instance in failed:
                    continue
                if act.kind == "sch-place":
                    lib, uuid = uuids.get(act.block_instance, ("", ""))
                    if not lib:
                        ok_all = False
                        failed.add(act.block_instance)
                        continue
                    place_args = []
                    holes = iter((lib, uuid))
                    for x in args:
                        place_args.append(next(holes) if x == "" else x)
                    # 撞号预防:请求名已被本轮先放件(含 block-apply 子件)占用 →
                    # 换同前缀下一空号,免吃异步注解改号(autoconnect 找不到件)。
                    if "--designator" in place_args:
                        i = place_args.index("--designator") + 1
                        want = place_args[i]
                        actual = want
                        if want in taken_desig:
                            prefix = want.rstrip("0123456789") or "U"
                            n = 1
                            while f"{prefix}{n}" in taken_desig:
                                n += 1
                            actual = f"{prefix}{n}"
                            place_args[i] = actual
                            renamed_desig[want] = actual
                            self.audit.event(
                                "designator-rename",
                                round_no=round_no,
                                instance=act.block_instance,
                                requested=want,
                                actual=actual,
                            )
                        taken_desig.add(actual)
                    # 变更类命令单发纪律(对抗评审):place 走 run_json 单发——
                    # rc=0 但 stdout 空/截断时 run_json 抛 AdapterError,盲重试
                    # (_run_json_retry)会在已落物上原参重发=孪生件;空回的补发
                    # 只走下方"认领坐实零落物"例外(同 block-apply 空回补发)。
                    try:
                        resp = self.adapter.run_json(place_args)
                    except AdapterError:
                        resp = {}
                    comp = (resp.get("result", {}) or {}).get("component", {}) or {}
                    desig = comp.get("designator", "")
                    if not desig:
                        # 空回包≠没放(审计 P1):回读认领,认领到=静默成功;
                        # 认领不到才重试一次(认领坐实"零落物",重发无双份
                        # 风险——与 block-apply 空回补发同一例外),仍空=真失败
                        _pg0 = act.page or "P1"
                        _want0 = (place_args[place_args.index("--designator") + 1]
                                  if "--designator" in place_args else "")
                        _cx = (float(place_args[place_args.index("--x") + 1])
                               if "--x" in place_args else 0.0)
                        _cy = (float(place_args[place_args.index("--y") + 1])
                               if "--y" in place_args else 0.0)
                        desig = self._claim_placed_component(_pg0, _want0, _cx, _cy)
                        if desig:
                            self.audit.event("sch-place-claim", round_no=round_no,
                                             instance=act.block_instance, designator=desig)
                        else:
                            try:
                                resp = self.adapter.run_json(place_args)
                            except AdapterError:
                                resp = {}
                            comp = (resp.get("result", {}) or {}).get("component", {}) or {}
                            desig = comp.get("designator", "")
                            if not desig:
                                desig = self._claim_placed_component(_pg0, _want0, _cx, _cy)
                    ok = bool(desig)
                    if ok:
                        placed_by_page.setdefault(act.page or "P1", {}).setdefault(act.block_instance, []).append(desig)
                    if ok and act.zone:
                        zone_designators.setdefault(act.page or "P1", {}).setdefault(act.zone, []).append(desig)
                    if ok and act.pinout:
                        ok = self._verify_pins(round_no, desig, act.pinout, act.page or "P1")
                        if not ok:
                            failed.add(act.block_instance)
                    if not ok:
                        ok_all = False
                        if not desig:
                            failed.add(act.block_instance)
                    self.audit.event(
                        "sch-place",
                        round_no=round_no,
                        instance=act.block_instance,
                        designator=desig or "?",
                        page=act.page or "P1",
                        ok=ok,
                    )
                    continue
                manifest: dict = {}
                if act.kind == "sch-autoconnect":
                    if renamed_desig and "--pin" in args:
                        i = args.index("--pin") + 1
                        desig_part, _, pin_part = args[i].partition(":")
                        if desig_part in renamed_desig:
                            args = list(args)
                            args[i] = f"{renamed_desig[desig_part]}:{pin_part}"
                    rc, _, aerr = self.adapter.run(args)
                    status = "applied" if rc == 0 else "failed"
                    # stderr 是 autoconnect 唯一的失败原因载体(ambiguous pin/
                    # 跨页撞号/无安全落点全在这)——两次诊断盲区后不再丢弃:
                    # 失败行必须可离线归因(req-07 usb_c/C1-C7 实证)。
                    if rc != 0:
                        manifest = {"failure": (aerr or "").strip()[-300:]}
                else:
                    # block-apply 非幂等:manifest 只从本次执行 stdout 解析,
                    # 绝不重发(重放=同页孪生再放一份,详见 _run_manifest_once)
                    manifest = self._run_manifest_once(args)
                    status = manifest.get("ok") or manifest.get("status") or "unknown"
                self.audit.event(
                    act.kind,
                    round_no=round_no,
                    instance=act.block_instance,
                    status=status,
                    failure=manifest.get("failure", "") or "",
                    window=getattr(self.adapter, "window_id", ""),
                    page=act.page or "P1",
                    args=args if act.kind in ("block-apply", "sch-place", "sch-autoconnect") else [],
                )
                if status != "applied":
                    if str(status).startswith("failed-partial"):
                        survivors = manifest.get("rollback", {}).get("survivedPrimitiveIds", [])
                        if survivors:
                            self.adapter.delete_primitives(survivors)
                            self.audit.event("cleanup", round_no=round_no, deleted=survivors)
                        try:
                            retry_args = self._jitter_at(args)
                            manifest = self._run_manifest_once(retry_args)
                            status = manifest.get("ok") or manifest.get("status") or "unknown"
                            self.audit.event(
                                act.kind,
                                round_no=round_no,
                                instance=act.block_instance,
                                status=status,
                                retry=True,
                                page=act.page or "P1",
                                args=retry_args if act.kind == "block-apply" else [],
                            )
                        except AdapterError as e:
                            self.audit.event(
                                "apply-fatal",
                                round_no=round_no,
                                instance=act.block_instance,
                                error=str(e)[:1500],
                            )
                if status == "applied":
                    des = [p["designator"] for p in manifest.get("placed", []) or [] if p.get("designator")]
                    if des:
                        placed_by_page.setdefault(act.page or "P1", {})[act.block_instance] = des
                        taken_desig.update(des)  # 子件自动排号入册,后续 place 避撞
                    if act.zone:
                        zone_designators.setdefault(act.page or "P1", {}).setdefault(act.zone, []).extend(des)
                if status != "applied":
                    ok_all = False
            except AdapterError as e:
                ok_all = False
                self.audit.event(
                    "apply-fatal",
                    round_no=round_no,
                    instance=act.block_instance,
                    error=str(e)[:2000],
                )
        if not ok_all and gate_report and gate_report.get("verdict") == "pass":
            ok_all = self._verify_substance(actions, round_no)
        return ok_all, gate_report

    def _verify_substance(self, actions, round_no: int) -> bool:
        """block-apply 的 failed-rolled-back 可能是回滚校验假象(部件实际在页上,gate 也过)。
        机械复核(逐页 --page 读,多页合并):计划网络全部存在于网表且页面非空 → 判 applied。"""
        comps: list[dict] = []
        page_nets: set[str] = set()
        try:
            for p in self._plan_pages(actions):
                read = self._run_json_retry(["sch", "read", "--page", p])
                res = read.get("result", {}) or {}
                comps.extend(c for c in res.get("components", []) if c.get("componentType") != "sheet")
                page_nets |= {str(n.get("net") or n.get("name") or "") for n in res.get("nets", [])}
        except AdapterError:
            return False
        planned = {
            act.args[i + 1].split("=", 1)[1]
            for act in actions
            for i, a in enumerate(act.args)
            if a == "--bind" and i + 1 < len(act.args)
        }
        for act in actions:
            if act.kind == "sch-autoconnect":
                try:
                    planned.add(act.args[act.args.index("--net") + 1])
                except ValueError:
                    pass
        missing = {n for n in planned if n and n.upper() != "NC" and n not in page_nets}
        from edaloop.validate.checks import _rail_family

        ir_families = {_rail_family(r.name or r.v_text()) for r in self.ir.power.rails}
        ir_families.add("GND|main")
        strong_missing = {
            n for n in missing if _rail_family(n) in ir_families or _rail_family(n).split("|")[0] == "GND"
        }
        ok = bool(comps) and len(comps) >= 10 and not strong_missing
        self.audit.event(
            "substance-verify",
            round_no=round_no,
            comps=len(comps),
            planned_nets=len(planned),
            strong_missing=sorted(strong_missing)[:15],
            weak_missing=sorted(missing - strong_missing)[:15],
            ok=ok,
        )
        return ok

    def _planned_nets_by_page(self, actions, page_of=None) -> dict[str, set[str]]:
        """从动作流提计划网(按页归组):block-apply 的 --bind PORT=NET 与
        autoconnect 的 --net(内部网 C*_N* 也走 autoconnect)。page_of 可覆写
        归页(freeze 重放相位用 inst_page,act.page 未改写)。"""
        planned: dict[str, set[str]] = {}
        for act in actions:
            pg = page_of(act) if page_of else (act.page or "P1")
            for i, a in enumerate(act.args):
                if a == "--bind" and i + 1 < len(act.args):
                    v = act.args[i + 1].split("=", 1)
                    if len(v) == 2 and v[1]:
                        planned.setdefault(pg, set()).add(v[1])
            if act.kind == "sch-autoconnect" and "--net" in act.args:
                planned.setdefault(pg, set()).add(act.args[act.args.index("--net") + 1])
        return planned

    def _snapshot_page_nets(self, page: str, round_no: int) -> None:
        """P0 网基线快照(收口后处理开始前):页内 part 引脚的 net 集。

        对抗评审(内部网盲区):upstream 块内部网 C*_N* 由上游模板落图时自连,
        不在动作流——_net_presence 的 planned 通道看不到它们;closeout 的
        group-move/arrange 拉移不保网(项目记忆定案),斩断已转换内部网的直连线
        后,缺陷C哨的快照窗口(compact 函数内)也已关,全部 P0 门静默。此基线
        在 _net_presence 里并入 planned:后处理结束仍零载体的网=确定性断网
        (缺陷C netmerge 形态——整网被并线吞掉——同样落网)。读失败不记基线
        (不可基线 ≠ 全缺,不制造假阳性)。"""
        try:
            comps, _deg = self._list_components(page)
        except Exception as e:  # noqa: BLE001
            self.audit.event("net-baseline-error", round_no=round_no, page=page,
                             error=str(e)[:80])
            return
        nets = {str(p.get("net") or "") for c in comps
                if c.get("componentType") == "part"
                for p in c.get("pins") or []}
        nets.discard("")
        if nets:
            self._pre_closeout_nets[page] = nets

    def _net_presence(self, actions, round_no: int,
                      page_of=None, pages: list[str] | None = None) -> list[dict]:
        """P0 net 存在性终检(审计):逐页 planned(动作流)对照 actual
        (sch read --page 的 nets),缺网=该页该网零载体=确定性断网,含跨页
        netport 配对(同一网在两页都 planned,任一页缺失即该页断)。

        生产路径由 run() 在 apply 后调用,结果经 self._net_missing 转
        Finding 阻断;冻结分支直接调用只进审计(目检裁决)。读失败页跳过
        并记弱告警(gate 兜底),不把连接器故障误判成断网。"""
        from edaloop.generate.adapter import AdapterError
        from edaloop.validate.checks import check_net_existence

        planned = self._planned_nets_by_page(actions, page_of)
        # 第三通道(对抗评审):收口前基线——planned(动作流)看不到 upstream
        # 内部网,closeout/compact 后零载体即断网(含 netmerge 整网被吞形态)
        for pg, nets in self._pre_closeout_nets.items():
            planned.setdefault(pg, set()).update(nets)
        if not planned:
            return []
        actual: dict[str, set[str]] = {}
        verified: dict[str, set[str]] = {}  # 只对核验过的页判缺失:读不到的页
        # 从 planned 里剔除——check_net_existence 对 actual 缺键的页会判全缺,
        # "不可核验"被误读成"零载体断网"=假 HALT
        for pg in pages if pages is not None else self._plan_pages(actions):
            try:
                read = self._run_json_retry(["sch", "read", "--page", pg])
            except AdapterError:
                self._layout_warnings.append({
                    "code": "NET_PRESENCE_UNVERIFIED",
                    "evidence": f"{pg} 页网表读失败,net 存在性终检跳过该页(gate 兜底)",
                })
                continue
            res = read.get("result", {}) if isinstance(read, dict) else {}
            if not isinstance(res, dict) or "nets" not in res:
                # 回包不是 sch read 形状(真机契约恒含 nets[],缺键=截断/异常
                # 通道):不可核验 ≠ 零载体,误判断网=假 HALT;跳过记弱告警
                self._layout_warnings.append({
                    "code": "NET_PRESENCE_UNVERIFIED",
                    "evidence": f"{pg} 页网表回包形状异常,net 存在性终检跳过该页",
                })
                continue
            actual[pg] = {str(n.get("net") or n.get("name") or "") for n in res.get("nets", [])}
            verified[pg] = planned.get(pg) or set()
        missing = check_net_existence(verified, actual)
        if missing:
            self.audit.event("net-presence", round_no=round_no, missing=missing)
        return missing

    def _probe_nets(self, tag: str, round_no: int, pages: list[str]) -> None:
        """freeze 相位网快照哨(2026-09-01,run-30c3833705a4/a116696cea07 定性):
        GND↔5V 并轨是**瞬态接触+网名粘死**——终态几何已无任何触点(脚-脚/
        锚-锚/锚-桩段全扫空),接触发生在某个移动/画线相位的中间态又被后续
        步骤分开,事后无法定位。逐相位记各页网名集,跑完按快照二分出并轨
        相位(autoconnect/reseat/closeout/compact/reseat2)。"""
        snap: dict[str, list[str]] = {}
        for pg in pages:
            try:
                read = self._run_json_retry(["sch", "read", "--page", pg])
            except Exception:  # noqa: BLE001
                snap[pg] = ["<read-fail>"]
                continue
            res = read.get("result", {}) if isinstance(read, dict) else {}
            snap[pg] = sorted({
                str(n.get("net") or n.get("name") or "")
                for n in (res.get("nets") or []) if n.get("net") or n.get("name")
            })
        self.audit.event("net-snapshot", round_no=round_no, tag=tag, nets=snap)

    def _repair_missing_nets(self, actions, round_no: int, inst_page: dict,
                             renamed_r: dict, missing: list[dict],
                             page_of=None, pages: list[str] | None = None) -> list[dict]:
        """freeze 缺网修复通道(2026-08-31,run-5a2ddef8a563 真机定性)。

        reseat/arrange 拉移后,规划网可被邻轨/自动名吞并:VIN 三页零载体且
        PMOSREV1:3=5V、ULNM3:4=GND、MCU1:21=3V3——引脚挂**错网**而非无网,
        reseat 只认「脚无网」,错脚有网=检测盲区。修复判据=autoconnect 动作
        的规划绑定(--pin D:N --net X,重放相位已按 renamed_r 换名):缺网页上
        凡规划该网的脚,实测网≠规划网 → disconnect 删错网残桩(平台真行为:
        清 net+删端点在该脚的全部导线+netport)→ _restub_net_pins 按计划网
        重落。上游块 ports_binding 的模板内部网(如 buck 的 C1_N*)不在
        动作流,不属本通道——记 unverified 交目检。返回复检后的余缺(仍只
        审计不阻断,目检裁决)。"""
        from edaloop.generate.adapter import AdapterError

        planned: dict[str, list[tuple[str, str]]] = {}  # page -> [(ref, net)]
        for act in actions:
            if act.kind != "sch-autoconnect" or act.block_instance not in inst_page:
                continue
            args = act.args
            if "--pin" not in args or "--net" not in args:
                continue
            ref = args[args.index("--pin") + 1]
            hit = renamed_r.get(act.block_instance)
            if hit and ref.partition(":")[0] == hit[0]:  # 重放同款换名翻译
                ref = f"{hit[1]}:{ref.partition(':')[2]}"
            planned.setdefault(inst_page[act.block_instance], []).append(
                (ref, args[args.index("--net") + 1]))
        by_page: dict[str, set[str]] = {}
        for m in missing:
            for n in m.get("missing") or []:
                by_page.setdefault(str(m.get("page")), set()).add(str(n))
        repaired: list[str] = []
        unverified: list[dict] = []
        for pg in sorted(by_page):
            want = by_page[pg]
            refs = {ref: net for ref, net in planned.get(pg, []) if net in want}
            for n in sorted(want - set(refs.values())):  # 逐网:无规划脚的网
                unverified.append({"page": pg, "nets": [n],
                                   "why": "no-planned-pin(upstream 模板网?)"})
            if not refs:
                continue
            if not self._open_page_for_edit(pg, "net-repair-open", round_no):
                unverified.append({"page": pg, "nets": sorted(want), "why": "open-failed"})
                continue
            try:
                comps, _deg = self._list_components(pg)
            except Exception as e:  # noqa: BLE001
                unverified.append({"page": pg, "nets": sorted(want),
                                   "why": f"list:{str(e)[:60]}"})
                continue
            parts = {c.get("designator"): c for c in comps
                     if c.get("componentType") == "part" and c.get("designator")}
            stubs: dict[str, list[tuple]] = {}
            for ref in sorted(refs):
                net = refs[ref]
                desig, _, pn = ref.partition(":")
                comp = parts.get(desig)
                pin = next((p for p in (comp.get("pins") or [])
                            if str(p.get("pinNumber")) == pn), None) if comp else None
                if pin is None or pin.get("x") is None:
                    unverified.append({"page": pg, "pin": ref, "net": net,
                                       "why": "pin-not-found"})
                    continue
                if str(pin.get("net") or "") == net:
                    continue  # 已对网(缺网的载体缺口在别处,不动这只脚)
                try:
                    self.adapter.run(["sch", "disconnect", "--pin", ref, "--doc", pg])
                except AdapterError:
                    pass  # 桩可能已不在(modify 回退同口径),重落兜底
                stubs.setdefault(desig, []).append(
                    (ref, float(pin["x"]), float(pin["y"]), net))
            # 电气端点避让(2026-09-01 并网护栏):重落桩不得与他件脚端点/
            # 他标记锚点重合——run-30c3833705a4 教训,端点重合=并网且粘死
            avoid_pts = [
                (float(p["x"]), float(p["y"]))
                for c in comps for p in (c.get("pins") or []) if p.get("x") is not None
            ] + [
                (float(f["x"]), float(f["y"]))
                for f in comps
                if f.get("componentType") in ("netport", "netflag", "netlabel")
                and f.get("x") is not None
            ]
            for desig, ss in stubs.items():
                self._restub_net_pins(pg, round_no, desig, parts[desig], ss,
                                      avoid_pts=avoid_pts)
                repaired.extend(f"{r}->{n}" for r, _x, _y, n in ss)
            if stubs:
                self._fix_marker_coincidences(pg, round_no)
        if repaired or unverified:
            self.audit.event("net-repair", round_no=round_no,
                             repaired=repaired, unverified=unverified)
        if not repaired:
            return missing
        try:
            return self._net_presence(actions, round_no, page_of=page_of, pages=pages)
        except Exception:  # noqa: BLE001
            return missing

    def _page_component_count(self, page: str) -> int:
        """sch read --page 回读非 sheet 器件数;读失败返回 -1(未知 ≠ 已清空)。

        连接器 wedge(DEGRADED/无响应/掉窗)不是页内容问题:refresh 钉扎+长等
        一回再读。清页验证是 落-量-清 的必经关卡,读死=整个 repack 回退
        (run-039f5a95e576 uln1 实证),值得给 wedge 一次耐心。"""
        try:
            read = self._run_json_retry(["sch", "read", "--page", page])
        except Exception as e:  # AdapterError 等:读不到按未知处理,绝不当作已清空
            if not self._connector_wedged(e):
                self.audit.event("clear-verify-error", page=page, error=str(e)[:500])
                return -1
            refresh = getattr(self.adapter, "refresh_window", None)
            if refresh:
                refresh()
            time.sleep(45)
            try:
                read = self._run_json_retry(["sch", "read", "--page", page], attempts=1)
            except Exception as e2:
                self.audit.event("clear-verify-error", page=page,
                                 wedge=True, error=str(e2)[:500])
                return -1
            self.audit.event("connector-wedge-recovered", page=page)
        res = read.get("result", {}) or {}
        return sum(1 for c in res.get("components", []) if c.get("componentType") != "sheet")

    def _repack_actions(self, actions, plan, round_no: int) -> bool:
        """两阶段布局(repack):真机试放定框 → 离线装箱 → 就地改写 actions 的 at/page。

        试放打在 P1(clear_all_pages 之后、正式落图之前)。上游 block-apply 把
        显式 --at 硬钳进图纸可用区(P9 实测 2026-08-27:--at 9000,4000 →
        640,760「出界是硬约束不是偏好」,无逃生旗标;清页不撤图纸几何),虚空
        网格位对它只是愿望——多块同页会被钳到同片纸内互叠,clusters 归属/量框
        全污。因此试放按「逐块 落-量-清」推进:单块独占画布(落点在哪无所谓,
        尺寸是平移不变量),拉拢+紧凑后 `sch clusters --json` 量该块 body/volume
        框,随即验证式清页给下一块腾地方。生产装箱 cell 用 volume(含 netport
        文字翼——正式页桩+文字照样落墨),目检框/尺寸标注用 body。

        装箱交 packer 离线完成,再改写 actions(block-apply 的 --at、各动作
        page;place/autoconnect 跟随其块),并按新页序稳定重排(--doc 切页
        粘性,减少前台来回摆)。

        任何一步异常返回 False:调用方继续用 compile 流式初值(旧行为),审计
        repack-fallback 记原因。
        """
        from edaloop.generate import packer
        from edaloop.generate.adapter import AdapterError
        from edaloop.generate.compile import ink_cells

        def _fallback(reason: str) -> bool:
            self.audit.event("repack-fallback", round_no=round_no, reason=reason)
            # 对抗评审:试放相位的恢复失败/线记账只对应随即清弃的 P1 试放画布
            # ——回退流式轮按生产判据从零计起(成功路径 return True 前有同款
            # 清零;不清零则试放残留可把无辜流式轮误判成 WIRE_RESTORE_BROKEN)
            self._wire_breaks = []
            self._wire_boxes.pop("P1", None)
            return False

        trial_blocks = [a for a in actions if a.kind == "block-apply"]
        if not trial_blocks:
            return _fallback("no-upstream-blocks")
        try:
            est = ink_cells(plan, self.catalog, spacing_default="250")
        except Exception as e:  # noqa: BLE001 —— 估算表异常同样回退流式
            return _fallback(f"ink:{type(e).__name__}:{str(e)[:80]}")
        missing = [a.block_instance for a in trial_blocks if a.block_instance not in est]
        if missing:
            return _fallback(f"ink-missing:{missing[0]}")
        # 试放画布保真(P0-3,2026-08-26):clear_all_pages 只清各窗口**活动页**,
        # P1 残件(上轮收尾前台停在末页 / 上次 freeze 的冻结层)会混进 clusters
        # 量框,freeze 连跑即逐轮累积——P1 184 件残骸、MCUSTM1/32 同坐标叠放
        # 的定性根因。试放前验证式清 P1;清不动回退流式,不在脏画布上量框
        # (框量错 = 装箱分页全错,宁退勿错)。
        if not self._clear_page_verified("P1", round_no):
            return _fallback("trial-canvas-uncleared:P1")
        # 阶段 1:试放(逐块 落-量-清)。--spacing 与正式一致(块内布局相同 →
        # box 平移不变);--at/--x/--y 仍写虚空网格位:sch place 不受钳、照单
        # 全收,block-apply 会被上游钳进纸内(见函数 docstring)——都无所谓,
        # 量的只是尺寸,量完即清。网格位仍在虚空带是为了 freeze 框画位(框必须
        # 画在纸外)。cell 四周 450 净空同时兜住目检框间距:框=实测 body 尺寸,
        # cell=est+2·PAD,est 偶尔低估也不至于让相邻框互叠(PAD=300 时 ldo3v3
        # 翼 324 越带的教训,run-47827896dd04)。
        _PAD = 450
        place_blocks = [a for a in actions if a.kind == "sch-place"]
        # place 通道块同样试放(真机 req-07 round2 定案:_PLACE_INK 估算对 netport
        # 拉伸失真,P2/P3 全部 overlap 都发生在 place 块 est 框与实测框之间)。
        # 预演 lib-search(读)→ sch-place(网格位 P1)→ autoconnect(P1,netport
        # 墨迹是翼展的大头),失败单块降级 est 不弃全局。
        _trial_names = {a.block_instance for a in trial_blocks} | {a.block_instance for a in place_blocks}
        # 带 x1=1500:A4 纸面右缘 1170 + 翼展 330 全部留在纸外虚空区——
        # 分步布局第一步的定义就是"纸内一个不放"(旧 x1=30 带起点在纸内,
        # 货架行下行排进纸面:13/37 块落纸内,真机 run-47827896dd04)。
        # 带满会开虚拟第 2 页且坐标复用,而试放只取 (gx,gy) 不看页号——
        # 溢出=两块同点真叠;带的虚拟宽度无实上限,溢出即加宽 12000 重排。
        _tcells = [packer.Cell(inst, est[inst][0] + _PAD * 2, est[inst][1] + _PAD * 2, 0, "trial")
                   for inst in _trial_names if inst in est]
        _tband = [1500, 30, 15000, 6030]
        for _ in range(4):
            grid = packer.pack(_tcells, band=tuple(_tband), keepout=None)
            if grid.pages <= 1:
                break
            _tband[2] += 12000
            self.audit.event("repack-trial-overflow", round_no=round_no,
                             band=tuple(_tband), pages=grid.pages)
        members: dict[str, list[str]] = {}
        renamed: dict[str, str] = {}
        comp_boxes: dict[str, dict] = {}  # 目检口径累计(body;重放改名自由号检查用)
        vol_boxes: dict[str, dict] = {}   # 生产口径累计(volume;同上)
        # 实例 → 实测 body 框(绝对坐标;量测时单块独占页,无跨块归属歧义)
        meas: dict[str, tuple[float, float, float, float]] = {}
        # 实例 → 实测 volume 框(绝对坐标,含 netport 文字翼):生产 cell 尺寸在
        # 量测时即定格,重放锚位校正同源——不受清页后位号复用/后续量测影响
        meas_vol: dict[str, tuple[float, float, float, float]] = {}
        trial_anchor: dict[str, tuple[float, float]] = {}  # 实例 → 实际生效原点(钳制感知)
        trial_failed: list[str] = []

        def _measure_module(inst: str, want: list[str]) -> bool:
            """单块量测 + 清场(2026-08-27 定案):上游把 block-apply --at 硬钳
            进图纸可用区,多块同页必被钳到同片纸内互叠(run-aa2412891d84:
            13/15 块堆 (640,760) 一带,9 对框重叠全因此来)。逐块独占让钳制
            落点无关紧要——尺寸是平移不变量;清页保证下一块独占画布。
            want 空(试放失败)也走清页:failed-partial 有残件,不清会污染
            下一块量测。清不动返回 False(宁退勿错,框量错=装箱全错)。"""
            if want:
                self._compact_internal_nets("P1", round_no, {inst: want})
                _rc, out, _err = self.adapter.run(
                    ["sch", "clusters", "--json", "--doc", "P1"])
                try:
                    rep = json.loads(out) if (out or "").strip() else {}
                except ValueError:
                    rep = {}
                cb = {c.get("designator"): (c.get("body") or c.get("box"))
                      for c in rep.get("clusters") or []
                      if c.get("designator") and (c.get("body") or c.get("box"))}
                vb = {c.get("designator"): c.get("box")
                      for c in rep.get("clusters") or []
                      if c.get("designator") and c.get("box")}
                comp_boxes.update(cb)
                vol_boxes.update(vb)
                mb = [cb[d] for d in want if d in cb]
                if len(mb) == len(want):
                    meas[inst] = (min(b["minX"] for b in mb),
                                  min(b["minY"] for b in mb),
                                  max(b["maxX"] for b in mb),
                                  max(b["maxY"] for b in mb))
                mv = [vb[d] for d in want if d in vb]
                if len(mv) == len(want):
                    meas_vol[inst] = (
                        min(b["minX"] for b in mv), min(b["minY"] for b in mv),
                        max(b["maxX"] for b in mv), max(b["maxY"] for b in mv))
            if not self._clear_page_verified("P1", round_no):
                self.audit.event("trial-measure-clear-failed", round_no=round_no,
                                 instance=inst)
                return False
            # 落-量-清把单位时间的画布变更率拉高了一个量级(每实例 apply+拉拢
            # +clear),正是上游 webview 主线程饿死向量(run-039f5a95e576 连接
            # 器 wedge 实证):块间歇给保存/重绘风暴留排水口(单测置 0)。
            time.sleep(self._MEASURE_PACE)
            return True

        for act in trial_blocks:
            _gp, gx, gy = grid.placements[act.block_instance]
            # 网格 cell 含 _PAD 净空,试放锚点回移半个 pad,让净空均匀落四边
            args = list(act.args)
            at = f"{gx + _PAD:.0f},{gy + _PAD:.0f}"
            had_at = "--at" in args
            if had_at:
                args[args.index("--at") + 1] = at
            else:
                args += ["--at", at]
            try:
                manifest = self._run_manifest_once(args + ["--doc", "P1"])
            except AdapterError as e:
                return _fallback(f"trial-adapter:{str(e)[:120]}")
            status = manifest.get("ok") or manifest.get("status") or "unknown"
            if status == "unknown":
                # 取证(run-fd3f51113bdc dc_in_u3 status=unknown → est cell 顶格
                # 上 oversize 页):区分"stdout 空回 {}"(键空)与"manifest 换了
                # 形状"(键非空但无 ok/status)。键空=上游假死/截断,换形=契约漂移,
                # 处置不同。raw 截断防审计膨胀。
                self.audit.event(
                    "trial-manifest-unknown", round_no=round_no,
                    instance=act.block_instance,
                    keys=sorted(manifest)[:20], raw=str(manifest)[:300])
            # manifest.origin = 实际生效原点(上游钳制时 relocated=True 且
            # x/y 是钳后位)——trial_anchor 记它,pack 重放偏移校正才不自欺
            org = manifest.get("origin") or {}
            try:
                ax, ay = float(org.get("x", gx + _PAD)), float(org.get("y", gy + _PAD))
            except (TypeError, ValueError):
                ax, ay = float(gx + _PAD), float(gy + _PAD)
            self.audit.event(
                "trial-apply", round_no=round_no, instance=act.block_instance,
                slot=f"{gx:.0f},{gy:.0f}", at=at, had_at=had_at,
                actual=f"{ax:.0f},{ay:.0f}", relocated=bool(org.get("relocated")),
                status=str(status)[:40],
            )
            trial_anchor[act.block_instance] = (ax, ay)
            if not str(status).startswith("applied"):
                # 单块失败不弃全局:该块 members 空 → 阶段 2 自然退估算 cell,
                # 其余块的实测框照常生效。applied-partial / applied-mismatch 的
                # 器件已落地,几何有效,照常收 members。
                # unknown 救援(run-fd3f51113bdc dc_in_u3 / run-88793706161c pwr_in:
                # stdout 空回 {} → est cell 顶格上 oversize 页、翼出纸):rc=0 但
                # manifest 空时若块已落地——只读回 P1 真件(sch list 过滤
                # componentType,clusters 的 marker 伪位号不算),量实测不量 est;
                # 零落物(apply 静默死)不重试不重放,est 兜底交重放相位。
                salvage: list[str] = []
                if status == "unknown":
                    try:
                        comps_s, _deg_s = self._list_components("P1")
                        salvage = [str(c.get("designator")) for c in comps_s
                                   if c.get("componentType") == "part" and c.get("designator")]
                    except Exception as e:  # noqa: BLE001
                        self.audit.event("trial-salvage-error", round_no=round_no,
                                         instance=act.block_instance,
                                         error=str(e)[:80])
                if salvage:
                    self.audit.event("trial-salvage", round_no=round_no,
                                     instance=act.block_instance, members=salvage,
                                     anchor="est-degraded")
                    members[act.block_instance] = salvage
                    if not _measure_module(act.block_instance, salvage):
                        return _fallback("trial-measure-clear:P1")
                    # 对抗评审(锚污染):manifest 空回=实际生效 origin 不可知
                    # (上游对 --at 恒硬钳进纸,钳位差可达 ~800)——trial_anchor
                    # 只剩请求虚空位,按它算 offsets 会让重放块整体逃出装箱
                    # cell。弃用实测锚,退 est+2*_EST_PAD 退化口径(尺寸略保守,
                    # 换锚映射自洽);实测尺寸仍在,仅锚校正不可信。
                    meas.pop(act.block_instance, None)
                    meas_vol.pop(act.block_instance, None)
                    trial_anchor.pop(act.block_instance, None)
                    continue
                if status == "unknown" and not salvage:
                    # 静默死(零落物)曾试过同参重试一次(run-0019e7efcfec):两块
                    # (pwr_in/uart0)重试照旧空回,不救测量反伤清页——重试把
                    # 落-量-清的画布变更率再翻倍,uart0 重试后 page-clear 两连
                    # rc=1(remaining=null,连接器过载形状)整个试放相位崩退
                    # streaming。定案:不重试,est 兜底(重放相位同块能正常落,
                    # 仅试放测量损失),静默死根因(深虚空 --at 的上游静默)交
                    # 连接器侧取证。
                    pass
                trial_failed.append(f"{act.block_instance}:{str(status)[:40]}")
                if not _measure_module(act.block_instance, []):
                    return _fallback("trial-measure-clear:P1")
                continue
            members[act.block_instance] = [
                p["designator"] for p in manifest.get("placed", []) or [] if p.get("designator")
            ]
            if not _measure_module(act.block_instance, members[act.block_instance]):
                return _fallback("trial-measure-clear:P1")
        uuids: dict[str, tuple[str, str]] = {}
        for act in actions:
            if act.kind != "lib-search":
                continue
            try:
                resp = self._run_json_retry(act.args)
            except AdapterError as e:
                # place 预演是降级路径:环境错误(如 --doc 确认失败)只丢该块
                # 实测,不弃全局——真机 2026-08-25 req-07 定案,sch place
                # --doc P1 曾拒绝(active-page 确认失败),此处曾整体崩。
                self.audit.event("trial-place-skip", round_no=round_no,
                                 instance=act.block_instance, error=str(e)[:150])
                continue
            lib, uuid = self._first_uuid(resp)
            if not lib and act.mpn and act.mpn.upper() != act.lcsc.upper():
                resp = self._run_json_retry(
                    ["lib", "search", "--query", act.mpn, "--limit", "3"]
                )
                lib, uuid = self._first_uuid(resp)
            if lib:
                uuids[act.block_instance] = (lib, uuid)
        for act in place_blocks:
            inst = act.block_instance
            lib, uuid = uuids.get(inst, ("", ""))
            hit = grid.placements.get(inst)
            if not lib or hit is None:
                trial_failed.append(f"{inst}:trial-place-skip")
                continue
            _gp, gx, gy = hit
            trial_anchor[inst] = (gx + _PAD, gy + _PAD)  # place 不受钳,请求即落位
            holes = iter((lib, uuid))
            args = [next(holes) if x == "" else x for x in act.args]
            for flag, val in (("--x", gx + _PAD), ("--y", gy + _PAD)):
                if flag in args:
                    args[args.index(flag) + 1] = f"{val:.0f}"
            args += ["--doc", "P1"]
            try:
                # 变更类单发纪律(同生产 place):盲重试会在已落物上重发=孪生件
                resp = self.adapter.run_json(args)
            except AdapterError as e:
                trial_failed.append(f"{inst}:trial-adapter:{str(e)[:60]}")
                if not _measure_module(inst, []):
                    return _fallback("trial-measure-clear:P1")
                continue
            comp = (resp.get("result", {}) or {}).get("component", {}) or {}
            desig = comp.get("designator", "")
            if not desig:
                trial_failed.append(f"{inst}:trial-place-no-desig")
                continue
            if "--designator" in act.args:
                want = act.args[act.args.index("--designator") + 1]
                if desig != want:
                    renamed[want] = desig  # 试放占号被改号:autoconnect --pin 同步换名
            members[inst] = [desig]
            # 本块 autoconnect 就地补齐再量(netport 墨迹是翼展大头,量框前
            # 必须落)——逐块推进与全量后补语义等价:桩按脚独立,无跨块依赖
            for ac in actions:
                if ac.kind != "sch-autoconnect" or ac.block_instance != inst:
                    continue
                ac_args = list(ac.args) + ["--doc", "P1"]
                if renamed and "--pin" in ac_args:
                    i = ac_args.index("--pin") + 1
                    d, _, p = ac_args[i].partition(":")
                    if d in renamed:
                        ac_args[i] = f"{renamed[d]}:{p}"
                try:
                    rc, _out, _err = self.adapter.run(ac_args)
                except AdapterError as e:
                    trial_failed.append(f"{inst}:trial-adapter:{str(e)[:60]}")
                    continue
                if rc != 0:
                    trial_failed.append(f"{inst}:trial-autoconnect")
            if not _measure_module(inst, members[inst]):
                return _fallback("trial-measure-clear:P1")
        _place_inst_set = {a.block_instance for a in place_blocks}
        for act in actions:  # 兜底:不属于任何 place 实例的 autoconnect 照旧跑
            if act.kind != "sch-autoconnect" or act.block_instance in _place_inst_set:
                continue
            args = list(act.args) + ["--doc", "P1"]
            if renamed and "--pin" in args:
                i = args.index("--pin") + 1
                d, _, p = args[i].partition(":")
                if d in renamed:
                    args[i] = f"{renamed[d]}:{p}"
            try:
                rc, _out, _err = self.adapter.run(args)
            except AdapterError as e:
                trial_failed.append(f"{act.block_instance}:trial-adapter:{str(e)[:60]}")
                continue
            if rc != 0:
                trial_failed.append(f"{act.block_instance}:trial-autoconnect")
        upstream_failed = {f.split(":", 1)[0] for f in trial_failed}
        if trial_blocks and all(a.block_instance in upstream_failed for a in trial_blocks):
            return _fallback(f"trial-apply-all:{trial_failed[0]}")
        self.audit.event(
            "repack-trial", round_no=round_no,
            blocks=len(trial_blocks) + len(place_blocks), failed=trial_failed,
        )
        # 阶段 2:定格 cells(尺寸在逐块量测时已记,见 _measure_module;装箱
        # cell 用 volume(含 netport 文字翼):正式页块模板的桩+文字照样落墨,
        # cell 含翼才保住"翼不越 cell + gap 归属安全距"的验证前提,不能收小。
        # v0.6.11 审计 P1 后画框也用 volume(框住全部墨迹);origin 审计仍记
        # body 尺寸作对照诊断)
        cells: list[packer.Cell] = []
        origin: dict[str, str] = {}
        # 亲和组名(审计 P2):planner 的 module 字段,同功能模块的块装箱聚拢同页;
        # plan 缺失/块未给 module → 空串=不分组,按带序排
        mod = {b.instance: b.module for b in (plan.blocks if plan else []) if b.module}
        offsets: dict[str, tuple[float, float]] = {}  # 实例 → 锚→volume框左下偏移
        place_insts = {a.block_instance for a in place_blocks}
        for act in list(trial_blocks) + list(place_blocks):
            inst = act.block_instance
            e = est[inst]  # 缺项已在阶段 1 拦截(ink-missing)
            kind = "upstream" if act.kind == "block-apply" else "place"
            mv = meas_vol.get(inst)
            if mv is not None:
                cells.append(packer.Cell(inst, mv[2] - mv[0], mv[3] - mv[1], e[2], kind,
                                         group=mod.get(inst, "")))
            else:
                # 成员对不上号(试放失败/manifest-clusters 口径漂移)→ 退估算,不弃全局;
                # est 框不含文字翼/桩线天然偏小,四周各垫 _EST_PAD 防翼越格挤邻块(审计 P1)
                cells.append(packer.Cell(inst, e[0] + 2 * _EST_PAD, e[1] + 2 * _EST_PAD,
                                         e[2], kind, group=mod.get(inst, "")))
                offsets[inst] = (-_EST_PAD, -_EST_PAD)
            m = meas.get(inst)
            if m is not None:
                origin[inst] = f"{m[2] - m[0]:.0f}x{m[3] - m[1]:.0f}"
            else:
                origin[inst] = f"est:{e[0]}x{e[1]}"
        for inst, e in est.items():
            if e[3] == "place" and inst not in place_insts:
                cells.append(packer.Cell(inst, e[0] + 2 * _EST_PAD, e[1] + 2 * _EST_PAD,
                                         e[2], "place"))
                offsets[inst] = (-_EST_PAD, -_EST_PAD)
                origin[inst] = f"est:{e[0]}x{e[1]}"
        # 量框审计在 freeze 分支之前发:冻结轮也要能从审计拿到 body 口径尺寸
        # (否则只能解析图上标注反推)
        self.audit.event("repack-measure", round_no=round_no, cells=origin)
        freeze_mode = os.environ.get("EDALOOP_LAYOUT_FREEZE", "")
        # 框位/偏移(两档冻结与生产重放通用):框画在虚空网格位(试放墨迹已随
        # 逐块量测清场,冻结页只留标注层);尺寸=实测 body。offsets = 实测
        # volume 框左下 − 实际生效原点(manifest origin,钳制感知)——装箱
        # cell 按 volume 定尺寸,重放锚=装箱位−offsets 才把体积框(含文字翼)
        # 整体钉进 cell;取 body 口径会让左侧翼伸进 gap、搭上邻块墨迹。
        box_of: dict[str, tuple[float, float, float, float, bool]] = {}
        for act in list(trial_blocks) + list(place_blocks):
            inst = act.block_instance
            hit = grid.placements.get(inst)
            ax = hit[1] + _PAD if hit else 0.0
            ay = hit[2] + _PAD if hit else 0.0
            m = meas.get(inst)
            if m is not None:
                box_of[inst] = (ax, ay, ax + (m[2] - m[0]), ay + (m[3] - m[1]), False)
                v = meas_vol.get(inst)
                oax, oay = trial_anchor.get(inst, (ax, ay))
                if v is not None:
                    offsets[inst] = (v[0] - oax, v[1] - oay)
            else:
                # 退化块(试放失败):框用估算(锚=试放网格位),标签带 est 前缀
                e = est[inst]
                box_of[inst] = (ax, ay, ax + e[0], ay + e[1], True)
        if freeze_mode:
            # 算法分步目检(用户验证序):"1"=试放页画框冻结;"pack"=全量装箱
            # A4 多页,每页真落块+框+标注后冻结。
            if freeze_mode != "pack":
                items = [(inst, *box) for inst, box in box_of.items()]
                self._freeze_trial_frames(round_no, items)
                raise TrialFreezeSignal("试放页已冻结供目检(EDALOOP_LAYOUT_FREEZE=1)")
            # ── pack 档:真·第二阶段(用户验证序第 2 步)——与生产 阶段3 同一
            # packer.pack(A4 行-货架页流)全量装箱,每页真落块+框+标注后冻结。
            # 装箱第 p 页 → P{p+1},从 P1 起(2026-08-31 用户定案:此前 P{p+2}
            # 留 P1 作试放标注层对比,交付页空一页)——与生产路径同式;试放
            # 墨迹在逐块量测时已清,P1 承载生产内容无冲突。重放锚 = 装箱位 −
            # offsets,体积框精确钉进 cell(裸锚会把翼展错位带进邻格)。旧
            # "BAND 目检填页"实验(gap 阶梯+仅第 0 页)已被本实现取代。cell_dim
            # 供成员对不上号时的"装箱预留槽"框。
            res = packer.pack(cells)
            cell_dim = {c.name: (c.w, c.h) for c in cells}
            self.audit.event(
                "repack-pack", round_no=round_no, pages=res.pages, oversize=res.oversize,
                waste=res.waste, note=res.note,
                placements={name: [f"P{p + 1}", round(x), round(y)]
                            for name, (p, x, y) in res.placements.items()},
            )
            tgt_pages = [f"P{p + 1}" for p in range(res.pages)]
            inst_page = {inst: f"P{p + 1}" for inst, (p, _x, _y) in res.placements.items()}
            # 目标页可能不存在(工程初始只有 P1):先建页再清再落,幂等——真机
            # run-57223f61a0bd 教训:不建页直接 --doc P2,重放 4/4 全失败。
            self._ensure_pages(tgt_pages, round_no)
            # P1 试放标注层只在「P1 非本轮生产页」时才可能画(freeze=1 档的
            # 对比层);P1 已是交付页时 KEEP_P1 同样不画——画了也会被下面的
            # 验证式清页抹掉或污染交付内容。必须画在页序重建之后(_ensure_pages
            # 对乱序工程重建 P 页会删旧建新,先画=白画)。
            if (os.environ.get("EDALOOP_LAYOUT_FREEZE_KEEP_P1", "") == "1"
                    and "P1" not in tgt_pages):
                items = [(inst, *box) for inst, box in box_of.items()]
                self._freeze_trial_frames(round_no, items)
            # 验证式清页(P0-3):freeze-pack 反复演练的正是"残骸累积"现场,
            # 裸 clear --doc 回包不说谎但也不保证结果;两趟仍不清只告警不冻结
            # 中止——目检模式宁叠勿断,残件证据留给目检者。
            for _pg in tgt_pages:
                if not self._clear_page_verified(_pg, round_no):
                    self._layout_warnings.append({
                        "code": "PAGE_CLEAR_FAILED",
                        "evidence": f"freeze-pack {_pg} 两趟未清净,重放层可能叠残件(目检模式宁叠勿断)",
                    })
            members_r: dict[str, list[str]] = {}
            replay_fail: list[str] = []
            for act in trial_blocks:  # upstream 块:block-apply 原样重放,锚=装箱位-偏移
                inst = act.block_instance
                if inst not in inst_page:
                    continue
                _pg, px, py = res.placements[inst]
                dx, dy = offsets.get(inst, (0.0, 0.0))
                args = list(act.args)
                at = f"{px - dx:.0f},{py - dy:.0f}"
                if "--at" in args:
                    args[args.index("--at") + 1] = at
                else:
                    args += ["--at", at]
                if inst in res.oversize and "--max-attempts" not in args:
                    # 图签默认让位后 oversize 块(759×791 的 vehicle_input 类)
                    # 在任何带图签页都过不了连接器 fitter 的 L 形适配(run-
                    # 3ece9f39e1f2 P7 拒放回执)——独占页 + 锚点直放,翼展压
                    # 图签角如实保留,目检裁决。fitter 拒绝在运行期表现为
                    # rc=0+空回("静默死"),--max-attempts 0 才肯落。
                    args += ["--max-attempts", "0"]
                try:
                    manifest = self._run_manifest_once(args + ["--doc", inst_page[inst]])
                except AdapterError as e:
                    replay_fail.append(f"{inst}:{str(e)[:60]}")
                    continue
                status = manifest.get("ok") or manifest.get("status") or "unknown"
                if status == "unknown" and not manifest:
                    # 空回静默死(rc=0、stdout 空、零落物——keys=[] 即此形,契约
                    # 漂移的 unknown keys 非空不试)。试放相位定案不重试(run-
                    # 0019e7efcfec:深虚空位+清页穿插,重试双倍负载还救不回);
                    # 重放相位不同:位在带内、无逐块清页穿插,run-86f0ec3ab850
                    # 实证同块试放死、重放活——零落物=无双份风险,补发一次。
                    self.audit.event("freeze-pack-replay-retry", round_no=round_no,
                                     instance=inst, page=inst_page[inst])
                    try:
                        manifest = self._run_manifest_once(args + ["--doc", inst_page[inst]])
                    except AdapterError as e:
                        replay_fail.append(f"{inst}:{str(e)[:60]}")
                        continue
                    status = manifest.get("ok") or manifest.get("status") or "unknown"
                if not str(status).startswith("applied"):
                    replay_fail.append(f"{inst}:{str(status)[:40]}")
                    continue
                members_r[inst] = [p["designator"] for p in manifest.get("placed", []) or []
                                   if p.get("designator")]
            # 键=块实例(值=(想要名,实际名)),不能按想要名键——两个块共用
            # 同一模板位号时(本次 uln2003_ch1/ch2 都要 ULN2003C)后者覆盖前者,
            # autoconnect 全解析到后一块的件上,前一块 0 连线(run-0cdc61dd3eea
            # ULN2003C1 全 16 脚空网、审计 ULN2003C2:1-4/13-16 假失败即此)。
            renamed_r: dict[str, tuple[str, str]] = {}
            # 重放已占用位号:平台对全工程重名会静默顺延改号(SWDHDR→
            # SWDHDR2、CMCUDEC→CMCUDEC1)且 place 回包回显请求名,回包落名
            # 不可信——place 前主动选自由名,回显即真实。位号全工程唯一,
            # used_r 跨页累计。
            used_r: set[str] = {d for ms in members_r.values() for d in ms}
            for act in actions:  # place 通道块:lib-search 结果复用,装箱位落放
                if act.kind != "sch-place" or act.block_instance not in inst_page:
                    continue
                inst = act.block_instance
                lib, uuid = uuids.get(inst, ("", ""))
                hit = res.placements.get(inst)
                if not lib or hit is None:
                    replay_fail.append(f"{inst}:replay-no-lib")
                    continue
                _pg, px, py = hit
                dx, dy = offsets.get(inst, (0.0, 0.0))
                holes = iter((lib, uuid))
                args = [next(holes) if x == "" else x for x in act.args]
                for flag, val in (("--x", px - dx), ("--y", py - dy)):
                    if flag in args:
                        args[args.index(flag) + 1] = f"{val:.0f}"
                args += ["--doc", inst_page[inst]]
                if "--designator" in args:
                    # 名字冲突防线:comp_boxes 是逐块量测的累计口径(连 marker
                    # 伪位号也含,SWDHDR1 即此来)——试放墨迹虽已清场、平台可能
                    # 回吐位号,但保守避开累计名无害;重放页已落的 used_r 更是
                    # 硬冲突。主动顺延到自由名再落,回包回显=真实落名,
                    # autoconnect/members_r 不再漂移(run-2dfa0434ad6f 实证)。
                    want = args[args.index("--designator") + 1]
                    free = want
                    if want in comp_boxes or want in used_r:
                        m = re.match(r"^(.*?)(\d+)$", want)
                        stem, n = (m.group(1), int(m.group(2)) + 1) if m else (want, 1)
                        while f"{stem}{n}" in comp_boxes or f"{stem}{n}" in used_r:
                            n += 1
                        free = f"{stem}{n}"
                        args[args.index("--designator") + 1] = free
                        self.audit.event("designator-rename", round_no=round_no,
                                         instance=inst, want=want, actual=free)
                    used_r.add(free)
                # 变更类命令单发纪律(对抗评审,同 _apply 生产 place):盲重试
                # (_run_json_retry)会在已落物上原参重发=孪生件;AdapterError
                # (含 rc=0 空回包)走认领坐实零落物后才允许补发一次。
                try:
                    resp = self.adapter.run_json(args)
                except AdapterError as e:
                    resp = {}
                    self.audit.event("freeze-pack-replay-error", round_no=round_no,
                                     instance=inst, error=str(e)[:100])
                desig = ((resp.get("result", {}) or {}).get("component", {}) or {}).get("designator", "")
                if not desig:
                    # 空回包≠没放(位号异步顺延/回显滞后):回读认领,认领到=
                    # 假阴性解除;认领不到才是真失败(审计 P1)。不盲重发——
                    # block-apply 重放相位"空回补发一次"定案的前提是零落物,
                    # place 这里用认领把"零落物"坐实,坐实后补发一次。
                    _cx = float(args[args.index("--x") + 1]) if "--x" in args else px - dx
                    _cy = float(args[args.index("--y") + 1]) if "--y" in args else py - dy
                    _want0 = args[args.index("--designator") + 1] if "--designator" in args else ""
                    desig = self._claim_placed_component(inst_page[inst], _want0, _cx, _cy)
                    if desig:
                        self.audit.event("freeze-pack-replay-claim", round_no=round_no,
                                         instance=inst, designator=desig)
                    else:
                        try:
                            resp = self.adapter.run_json(args)
                        except AdapterError:
                            resp = {}
                        desig = ((resp.get("result", {}) or {}).get("component", {}) or {}).get("designator", "")
                        if not desig:
                            desig = self._claim_placed_component(inst_page[inst], _want0, _cx, _cy)
                            if desig:
                                self.audit.event("freeze-pack-replay-claim", round_no=round_no,
                                                 instance=inst, designator=desig)
                if not desig:
                    replay_fail.append(f"{inst}:replay-no-desig")
                    continue
                if "--designator" in act.args:
                    want = act.args[act.args.index("--designator") + 1]
                    if desig != want:
                        renamed_r[inst] = (want, desig)
                members_r[inst] = [desig]
            ac_fail: list[str] = []
            for act in actions:  # 各页 autoconnect 预演同步换名
                if act.kind != "sch-autoconnect" or act.block_instance not in inst_page:
                    continue
                args = list(act.args) + ["--doc", inst_page[act.block_instance]]
                if renamed_r and "--pin" in args:
                    i = args.index("--pin") + 1
                    d, _, pnp = args[i].partition(":")
                    hit = renamed_r.get(act.block_instance)
                    if hit and d == hit[0]:
                        args[i] = f"{hit[1]}:{pnp}"
                pin = args[args.index("--pin") + 1] if "--pin" in args else act.block_instance
                try:
                    rc, _o, e = self.adapter.run(args)
                    if rc != 0:  # 目检模式:连线失败不阻断画框,但必须入审计
                        ac_fail.append(f"{pin}:{(e or '')[:40]}")
                except AdapterError as e:
                    ac_fail.append(f"{pin}:{str(e)[:40]}")
            if ac_fail:
                self.audit.event("freeze-pack-autoconnect", round_no=round_no, failed=ac_fail)
            self._probe_nets("post-autoconnect", round_no, tgt_pages)
            # 目检质量两关(都在紧凑化之前——直连线端点继承这时的几何):
            # ①对脚旋转消 U 形绕行 ②越带兜底桩重落(run-fc264cf3ac76 目检)
            for _pg in tgt_pages:
                self._rotate_outward_pins(_pg, round_no)
                self._reseat_escape_marks(_pg, round_no)
            self._probe_nets("post-reseat1", round_no, tgt_pages)
            # closeout(出纸钳回带)必须在 compact 之前:钳移移动组件,若先
            # compact,记下的直连线 bbox 全部陈旧(审计 P1 出纸钳制接入点);
            # oversize 页豁免——块高出 A4 带是装箱定案不是缺陷,钳它=错。
            _oversize_pgs = {inst_page[n] for n in res.oversize if n in inst_page}
            self._arrange_closeout(
                round_no,
                {pg: {i2: ms for i2, ms in members_r.items() if inst_page.get(i2) == pg}
                 for pg in tgt_pages if pg not in _oversize_pgs},
                {})
            self._probe_nets("post-closeout", round_no, tgt_pages)
            for _pg in tgt_pages:
                # 重放后同样紧凑化:各页实测框(蓝框)与 P1 同口径
                self._compact_internal_nets(
                    _pg, round_no,
                    {i2: ms for i2, ms in members_r.items() if inst_page.get(i2) == _pg})
            self._probe_nets("post-compact", round_no, tgt_pages)
            for _pg in tgt_pages:
                # 紧凑化后复探+分离(同生产收口序:compact 拉近在 closeout 之后
                # 仍移动器件,收口探针看不见末端几何),分离后再补一轮压体/
                # 斜甩标记重落(rotate/reseat 首轮在重放后、closeout 前已跑)
                self._overlap_reprobe(
                    _pg, round_no,
                    {i2: ms for i2, ms in members_r.items() if inst_page.get(i2) == _pg},
                    oversize=_pg in _oversize_pgs)
                self._reseat_escape_marks(_pg, round_no)
                # 同侧扫尾(终态标记最后变异源是 reseat2 的 fallback 盲落):
                # 引脚对侧的同网近距标记拆掉按外侧优先序重落
                self._fix_wrong_side_marks(_pg, round_no)
                # 末轮 reseat 后终态复探(2026-09-01 布局治本批,「顺序即盲区」
                # 二次实证):reseat2/同侧扫尾/盲退护栏仍在探针后落标记+拆落
                # 标记,末端几何此前无人看;复探分离本体级相交后,组移拖斜的
                # 标记再收口一轮(标记不动器件 → 复探→reseat 有界收敛,无递归)
                self._overlap_reprobe(
                    _pg, round_no,
                    {i2: ms for i2, ms in members_r.items() if inst_page.get(i2) == _pg},
                    oversize=_pg in _oversize_pgs)
                self._reseat_escape_marks(_pg, round_no)
            self._probe_nets("post-reseat3", round_no, tgt_pages)
            self.audit.event("freeze-pack-replay", round_no=round_no,
                             pages={pg: sorted(i2 for i2, p2 in inst_page.items() if p2 == pg)
                                    for pg in tgt_pages},
                             failed=replay_fail)
            for _pg in tgt_pages:
                _rc, out2, _err = self.adapter.run(["sch", "clusters", "--json", "--doc", _pg])
                try:
                    rep2 = json.loads(out2) if (out2 or "").strip() else {}
                except ValueError:
                    rep2 = {}
                # 框口径 = volume(box:器件+自有 marker 文字+桩线)优先,body 只是
                # 退路(审计 P1/FRM-1:body 只并集器件本体,netport 文字/netflag/
                # 桩线全在框外——"框不住墨迹"目检问题的根因;重放后 clusters
                # 实测的就是这个口径,直接取用)
                comp_boxes2 = {c.get("designator"): (c.get("box") or c.get("body"))
                               for c in rep2.get("clusters") or []
                               if c.get("designator") and (c.get("box") or c.get("body"))}
                # 本页自画直连线 bbox(紧凑化产物,不属任何 cluster volume):
                # 框 = volume ∪ 自画线,才覆盖块内全部墨迹(用户口径:框必须
                # 框住连线网格与最远处文字)。跨块线两端块的框都并——框交叠
                # 是"墨迹相连"的诚实呈现,不交叠=漏墨迹。
                wb2: dict[str, list[float]] = {}
                for _ia, _ib, _bb in self._wire_boxes.get(_pg, []):
                    for i2 in {_ia, _ib}:
                        if inst_page.get(i2) != _pg:
                            continue
                        w = wb2.setdefault(i2, [1e9, 1e9, -1e9, -1e9])
                        w[0] = min(w[0], _bb[0])
                        w[1] = min(w[1], _bb[1])
                        w[2] = max(w[2], _bb[2])
                        w[3] = max(w[3], _bb[3])
                items2: list[tuple] = []
                for inst in [i2 for i2, p2 in inst_page.items() if p2 == _pg]:
                    want = members_r.get(inst, [])
                    mb2 = [comp_boxes2[d] for d in want if d in comp_boxes2]
                    if want and len(mb2) == len(want):
                        fx1 = min(b["minX"] for b in mb2)
                        fy1 = min(b["minY"] for b in mb2)
                        fx2 = max(b["maxX"] for b in mb2)
                        fy2 = max(b["maxY"] for b in mb2)
                        w = wb2.get(inst)
                        if w and w[0] < 1e9:
                            fx1, fy1 = min(fx1, w[0]), min(fy1, w[1])
                            fx2, fy2 = max(fx2, w[2]), max(fy2, w[3])
                        items2.append((inst, fx1, fy1, fx2, fy2, False))
                    else:
                        # 成员对不上号 → 画"装箱预留槽"框:尺寸用装箱时的 cell 而非
                        # 估算 ink(run-83ecf3862c01 教训:锚是按实测 cell 装的,套估算
                        # 尺寸会让框越 BAND 顶、罩进邻块器件)。
                        _p, px, py = res.placements[inst]
                        cw, ch = cell_dim.get(inst, (est[inst][0], est[inst][1]))
                        items2.append((inst, px, py, px + cw, py + ch, True))
                self._freeze_trial_frames(round_no, items2, page=_pg)
            # P1 收尾清层(审计 P2):上一轮 freeze=1 留在 P1 的试放标注层是
            # 过程产物,freeze-pack 交付前清掉;KEEP_P1=1 保留(且本轮标注层
            # 也只在 KEEP 下画)。P1 已是本轮生产交付页时不清(清=毁交付物)。
            # 两趟不清只告警,与 freeze-pack 页清同口径。
            if ("P1" not in tgt_pages
                    and os.environ.get("EDALOOP_LAYOUT_FREEZE_KEEP_P1", "") != "1"):
                if not self._clear_page_verified("P1", round_no):
                    self._layout_warnings.append({
                        "code": "PAGE_CLEAR_FAILED",
                        "evidence": "freeze-pack P1 标注层两趟未清净(过程产物残留,目检裁决)",
                    })
            # P0 net 存在性终检(冻结分支审计+修复,余缺目检裁决):重放页按
            # inst_page 归页(actions 的 act.page 在冻结相位未改写)。缺网先走
            # 修复通道(错网残桩 disconnect+按计划网重落,run-5a2ddef8a563
            # 定性的「拉移并轨」形态),余缺才交目检
            try:
                _miss = self._net_presence(
                    actions, round_no,
                    page_of=lambda a: inst_page.get(a.block_instance) or a.page or "P1",
                    pages=tgt_pages)
                if _miss:
                    self._repair_missing_nets(
                        actions, round_no, inst_page, renamed_r, _miss,
                        page_of=lambda a: inst_page.get(a.block_instance) or a.page or "P1",
                        pages=tgt_pages)
            except Exception:  # noqa: BLE001
                pass
            raise TrialFreezeSignal(
                f"装箱 {res.pages} 页(A4,行-货架)全量重放并画框冻结;"
                f"重放失败={replay_fail or '无'}")
        # 阶段 3:离线装箱(纯几何,页内硬保证不重叠)
        try:
            res = packer.pack(cells)
        except Exception as e:  # noqa: BLE001
            return _fallback(f"pack:{type(e).__name__}:{str(e)[:100]}")
        self.audit.event(
            "repack-pack", round_no=round_no, pages=res.pages, oversize=res.oversize,
            waste=res.waste, note=res.note,
            placements={
                name: [f"P{p + 1}", round(x), round(y)]
                for name, (p, x, y) in res.placements.items()
            },
        )
        # oversize 页集给 validate:几何族 fail 降弱观察(交付 review),电气族照 blocking
        self._repack_oversize_pages = {
            f"P{res.placements[name][0] + 1}" for name in res.oversize
        }
        # 改写:锚/页名;锚 = 装箱位 − offsets(试放实测 origin 到 body 最小角的
        # 偏移)——裸写 (x,y) 会把体积翼展整体平移错位、侵入邻格(与 freeze=pack
        # 重放同一条公式)。place 通道的 --x/--y 同样要改写,否则墨迹留在试放
        # 虚空坐标。lib-search 无页字段,按其块实例的新页参与排序。
        inst_page = {inst: f"P{p + 1}" for inst, (p, _x, _y) in res.placements.items()}
        for act in actions:
            if act.block_instance in res.placements:
                p, x, y = res.placements[act.block_instance]
                act.page = f"P{p + 1}"
                dx, dy = offsets.get(act.block_instance, (0.0, 0.0))
                if act.kind == "block-apply" and "--at" in act.args:
                    act.args[act.args.index("--at") + 1] = f"{x - dx:.0f},{y - dy:.0f}"
                    if act.block_instance in res.oversize and "--max-attempts" not in act.args:
                        # 同 freeze 分支(fitter 拒放=rc=0 空回静默死):oversize
                        # 块独占页锚点直放,--max-attempts 0 才肯落;生产重放漏补
                        # = oversize 块整页缺失烧 GATE_FAIL(对抗评审)
                        act.args += ["--max-attempts", "0"]
                elif act.kind == "sch-place":
                    for flag, val in (("--x", x - dx), ("--y", y - dy)):
                        if flag in act.args:
                            act.args[act.args.index(flag) + 1] = f"{val:.0f}"
        page_rank = {f"P{p + 1}": p for p in range(res.pages)}

        def _rank(a) -> int:
            if a.kind == "sch-gate":
                return res.pages + 1
            return page_rank.get(a.page or inst_page.get(a.block_instance) or "", res.pages)

        actions.sort(key=_rank)  # 稳定排序:页内产出序保持(lib-search 仍在 place 前)
        # 试放相位(P1 落-量-清)的紧凑化恢复失败只污染随即清弃的试放画布,
        # 不计入生产判据:重放前清零,validate 的 WIRE_RESTORE 门只看生产页
        self._wire_breaks = []
        return True

    def _list_components(self, page: str) -> tuple[list[dict], str]:
        """sch list 逐级降载读取(1MB stdout 截断防线,P0-2)。

        真机 run-1dff13dad148:P1 184 件时 `--include-pins --include-bbox`
        输出在 1048576 字节(管道上限)被截,json.loads 报 Invalid control
        character,紧凑化整页静默跳过。截断特征 = 解析失败且 len(out) ≥
        900_000 → 降载重试(去 --include-bbox;本体框由调用方用自件引脚
        凸包近似);再截断继续降(无引脚面数据,紧凑化/拉近无从下手 → 抛错
        交上层 fail-soft)。输出短的解析失败 = 真坏,原样抛。降级成功记
        SCH_LIST_TRUNCATED 弱告警(轮末随 validate 显性化),按页去重。

        返回 (components, degraded):degraded="no-bbox" 表示本体框缺失;
        抛 ValueError 表示读到头也没拿到可用的引脚面。
        """
        variants = (
            ["sch", "list", "--page", page, "--include-pins", "--include-bbox"],
            ["sch", "list", "--page", page, "--include-pins"],
        )
        note = ""
        for i, args in enumerate(variants):
            _rc, out, _err = self.adapter.run(args)
            try:
                rep = json.loads(out) if (out or "").strip() else {}
                comps = rep.get("result", {}).get("components") or []
                if not comps:
                    raise ValueError("empty components")
            except ValueError:
                if len(out or "") < 900_000:
                    raise
                note = f"len={len(out or '')}"
                self.audit.event("list-degraded", page=page, variant=i, note=note)
                continue
            if i > 0:
                w = {
                    "code": "SCH_LIST_TRUNCATED",
                    "evidence": f"{page} 页 sch list 超 1MB 截断,已降载读取(去 --include-bbox;"
                                f"本体框用引脚凸包近似,穿框约束弱于实测)",
                }
                if not any(x["code"] == "SCH_LIST_TRUNCATED" and page in x["evidence"]
                           for x in self._layout_warnings):
                    self._layout_warnings.append(w)
                return comps, "no-bbox"
            return comps, ""
        raise ValueError(f"truncated:{note or 'empty'}")

    def _claim_placed_component(self, page: str, want: str, x: float, y: float) -> str:
        """place 静默成功认领(审计 P1):rc=0 但回包无 designator 时,回读
        页面组件把"实际落没落"变成真结论。

        为什么:平台 place 回包回显不可信——位号被异步注解顺延后回包仍抄
        请求名;更糟的是空回包不等于没放(真机实证:place 落物成功、回包
        component 空壳,"place 无回=没放"是假阴性,重放整块假失败)。
        认领序:请求名精确命中 → bbox 包含落点锚(锚是 part 原点,bbox 含
        本体渲染) → 中心最近且 ≤80(装箱格距 ≥100,近邻不会更近)。全部
        落空才是真没放。读失败返回 ""(调用方按原口径处理)。"""
        try:
            comps, _deg = self._list_components(page)
        except Exception:  # noqa: BLE001
            return ""
        if want:
            for c in comps:
                if str(c.get("designator") or "") == want:
                    return want
        best_d, best = 1e18, ""
        for c in comps:
            b = c.get("bbox")
            if not (isinstance(b, dict) and "minX" in b):
                continue
            x1, y1, x2, y2 = (float(b["minX"]), float(b["minY"]),
                              float(b["maxX"]), float(b["maxY"]))
            if x1 - 2 <= x <= x2 + 2 and y1 - 2 <= y <= y2 + 2:
                return str(c.get("designator") or "")
            d = abs((x1 + x2) / 2 - x) + abs((y1 + y2) / 2 - y)
            if d < best_d:
                best_d, best = d, str(c.get("designator") or "")
        return best if best_d <= 80.0 else ""

    def _groups_of(self, page: str) -> list[tuple[str, list[str]]]:
        """当前组表 → [(gid, [designator...])];读失败返回 [](调用方放弃组操作)。"""
        _, out, _ = self.adapter.run(["sch", "group", "list", "--json", "--doc", page])
        try:
            rep = json.loads(out) if (out or "").strip() else {}
            groups = [g for gs in (rep.get("groupsByPage") or {}).values() for g in gs]
        except ValueError:
            return []
        out_l: list[tuple[str, list[str]]] = []
        for g in groups:
            gid = g.get("id") or g.get("name") or ""
            members = [m.get("designator") for m in (g.get("members") or [])]
            if gid and members:
                out_l.append((gid, [m for m in members if m]))
        return out_l

    def _isolate_designator(self, page: str, round_no: int, designator: str) -> str | None:
        """位号 → 单件组 id(供 group-move 刚移;P1-6 对伴拉近用)。

        已在单件组直接用;在多件组拆组重封(同 `_shatter_groups` 语义:点名件
        单件化,余件重组,不多不少);完全无组建单件组。create 不回组 id,重列
        取回(以重列为准,防"同一位号只属一个组"竞态)。组命令是变更类:单发
        不重试,任何一步后重列拿不到单件组 → None(调用方放弃该次拉近)。
        """
        hit = None
        for gid, members in self._groups_of(page):
            if designator in members:
                hit = (gid, members)
                break
        if hit is not None:
            gid, members = hit
            if members == [designator]:
                return gid
            rest = [m for m in members if m != designator]
            self.adapter.run(["sch", "group", "ungroup", "--group", gid, "--doc", page])
            self.adapter.run(["sch", "group", "create", "--members", designator, "--doc", page])
            if len(rest) >= 2:
                self.adapter.run(["sch", "group", "create", "--members", ",".join(rest), "--doc", page])
            elif rest:
                self.adapter.run(["sch", "group", "create", "--members", rest[0], "--doc", page])
            self.audit.event("pull-isolate", round_no=round_no, page=page,
                             designator=designator, group=gid, rest=rest)
        else:
            self.adapter.run(["sch", "group", "create", "--members", designator, "--doc", page])
        for gid, members in self._groups_of(page):
            if members == [designator]:
                return gid
        return None

    @staticmethod
    def _page_electrical_map(comps) -> dict:
        """页内电气几何图:点(脚端点+标记锚)与段(脚↔伴生标记的桩线)。

        供移动前触点预检(_move_touch_conflict)。group-move 拖着桩线+标记
        整组平移,终位上任一动电气点落上他网点(共端点)或他网段(T 结)
        即并网——pin.net 是存储属性,接触瞬间翻写、后续几何分离不回滚,
        电源网 isGlobal 项目级一点并轨全工程塌(run-b2c1990f44a4 相位快照
        哨定性:post-closeout 五页 GND 全在 → post-compact 全灭,而窗口内
        compact-netmerge 零事件 = 并轨源在窗口前的 _pull_long_pairs 移动)。
        标记归属:同网锚距脚 ≤120 视为该件伴生标记(与紧凑化桩线同口径)。"""
        pts: list[list] = []   # [x, y, net, owner]
        for c in comps:
            if c.get("componentType") != "part" or not c.get("designator"):
                continue
            d = str(c["designator"])
            for p in c.get("pins") or []:
                if p.get("x") is not None and p.get("net"):
                    pts.append([float(p["x"]), float(p["y"]),
                                str(p["net"]), d])
        marks: list[list] = []  # [x, y, net, owner|""]
        for m in comps:
            if m.get("componentType") not in ("netport", "netflag", "netlabel") \
                    or m.get("x") is None:
                continue
            mn = str(m.get("net") or m.get("name") or "")
            if not mn:
                continue
            mx, my = float(m["x"]), float(m["y"])
            owner = next((d for px, py, pn, d in pts
                          if pn == mn and abs(px - mx) + abs(py - my) <= 120.0), "")
            marks.append([mx, my, mn, owner])
        segs: list[list] = []   # [x1, y1, x2, y2, net, owner]
        for mx, my, mn, owner in marks:
            if not owner:
                continue
            for px, py, pn, d in pts:
                if d == owner and pn == mn \
                        and abs(px - mx) + abs(py - my) <= 120.0:
                    segs.append([px, py, mx, my, mn, owner])
        return {"pts": pts, "marks": marks, "segs": segs}

    def _move_touch_conflict(self, mover: str, dx: float, dy: float,
                             emap: dict) -> bool:
        """mover 平移 (dx,dy) 后的电气点/段 与他网点/段是否构成并网触点
        (异网才计:同名网接触是 netport 语义本身,无害)。共端点(≤4)与
        点落段上(T 结,≤2.5)双向都查——他网端点落上移动后的桩段同样并线。"""
        mov_pts = [[x + dx, y + dy, n] for x, y, n, d in emap["pts"] if d == mover]
        mov_pts += [[x + dx, y + dy, n] for x, y, n, d in emap["marks"]
                    if d == mover]
        mov_segs = [[a + dx, b + dy, c + dx, e + dy, n]
                    for a, b, c, e, n, d in emap["segs"] if d == mover]
        for_pts = [(x, y, n) for x, y, n, d in emap["pts"] if d != mover]
        for_pts += [(x, y, n) for x, y, n, d in emap["marks"] if d != mover]
        for_segs = [(a, b, c, e, n) for a, b, c, e, n, d in emap["segs"]
                    if d != mover]
        for mx, my, mn in mov_pts:
            for fx, fy, fn in for_pts:
                if mn != fn and abs(mx - fx) <= 4.0 and abs(my - fy) <= 4.0:
                    return True
            for a, b, c, e, fn in for_segs:
                if mn != fn and _leg_near_point(a, b, c, e, mx, my, 2.5):
                    return True
        for fx, fy, fn in for_pts:
            for a, b, c, e, mn in mov_segs:
                if mn != fn and _leg_near_point(a, b, c, e, fx, fy, 2.5):
                    return True
        return False

    def _pull_long_pairs(self, page: str, round_no: int,
                         members: dict[str, list[str]]) -> int:
        """对伴拉近(P0-1/P1-6):2 脚同块内部网线距超线长门时,把 ≤4 脚的
        小件刚移到大件(锚)脚旁 ≤190,让直连线从此可画。

        块模板的 relational 布局把对伴(CC 下拉 R11/R49、串联对)放在主芯片
        侧而非连接器脚旁,转真线就是 500-900 长横线(P1 J3:A5↔R11 875 /
        P2 J5:A5↔R49 510,2026-08-26 定案)。锚=脚多者(平局按位号序),
        落点扫描:锚件中心→锚脚向外扇区优先,再四轴四象限 × 距离 90/140/190,
        不压任何他件框(余量 15)、不出图(≥12);group-move 刚移,桩线+远端
        netport 自动跟随(上游 move 内核带电气对账,失败自动恢复),被拒时回退
        sch modify 改位(纸外虚空区唯一可行路,先拆桩防孤儿),移动后由
        后续紧凑化按新几何画线。本地几何缓存同步平移,后续网用新位判断。
        任何失败只审计不中断。返回拉近次数(每页上限 _PULL_MAX)。
        """
        from edaloop.generate.adapter import AdapterError  # 就近:except 引用必须本地可见

        try:
            comps, _deg = self._list_components(page)
        except Exception as e:  # noqa: BLE001
            self.audit.event("pull-close", round_no=round_no, page=page,
                             error=f"list:{str(e)[:80]}")
            return 0
        # 电气几何图(移动触点预检用):拉移拖着桩+标记平移,终位共端点/T结
        # 即并网粘死。拉后同步平移,后续拉的预检用新位。
        emap = self._page_electrical_map(comps)

        def _emap_translate(d: str, ddx: float, ddy: float) -> None:
            for row in emap["pts"] + emap["marks"]:
                if row[3] == d:
                    row[0] += ddx
                    row[1] += ddy
            for row in emap["segs"]:
                if row[5] == d:
                    row[0] += ddx
                    row[1] += ddy
                    row[2] += ddx
                    row[3] += ddy

        def _avoid_pts() -> list[tuple[float, float]]:
            return [(r[0], r[1]) for r in emap["pts"] + emap["marks"]]
        des_block: dict[str, str] = {}
        for inst, desigs in members.items():
            for d in desigs:
                des_block[d] = inst
        parts = {c.get("designator"): c for c in comps
                 if c.get("componentType") == "part" and c.get("designator")}
        nets: dict[str, list[tuple[str, float, float]]] = {}
        for d, c in parts.items():
            if d not in des_block:
                continue
            for p in c.get("pins") or []:
                n = p.get("net")
                if n and _INTERNAL_NET_RE.match(str(n)) and p.get("x") is not None:
                    nets.setdefault(str(n), []).append((d, float(p["x"]), float(p["y"])))

        def _bbox(c: dict) -> tuple[float, float, float, float] | None:
            b = c.get("bbox")
            if isinstance(b, dict) and "minX" in b:
                return (float(b["minX"]), float(b["minY"]), float(b["maxX"]), float(b["maxY"]))
            xs = [float(p["x"]) for p in (c.get("pins") or []) if p.get("x") is not None]
            ys = [float(p["y"]) for p in (c.get("pins") or []) if p.get("x") is not None]
            if not xs:
                return None
            return (min(xs) - 10, min(ys) - 10, max(xs) + 10, max(ys) + 10)

        def _pin_count(c: dict) -> int:
            return len([p for p in (c.get("pins") or []) if p.get("x") is not None])

        boxes = {d: _bbox(c) for d, c in parts.items()}

        def _find_slot(ax: float, ay: float, ab, mb, mover_d: str,
                       mx: float, my: float):
            """锚脚旁找空位:锚件中心→锚脚向外射线优先(贴脚=沿引脚朝向),
            再四轴、四象限;全 5 网格吸附,等距取移动量最小的落点。返回
            (dx,dy) 或 None(无空位)。mx,my = 移动件参照脚(用于算平移量)。"""
            cx, cy = (ab[0] + ab[2]) / 2, (ab[1] + ab[3]) / 2
            vx, vy = ax - cx, ay - cy
            vlen = math.hypot(vx, vy)
            v = (vx / vlen, vy / vlen) if vlen > 1 else (1.0, 0.0)
            sx, sy = (1.0 if v[0] >= 0 else -1.0), (1.0 if v[1] >= 0 else -1.0)
            dir_cands: list[tuple[float, float]] = []
            for dv in (v, (sx, 0.0), (0.0, sy), (sx, sy), (-sx, 0.0),
                       (0.0, -sy), (-sx, sy), (sx, -sy)):
                key = (round(dv[0], 3), round(dv[1], 3))
                if key not in {(round(a[0], 3), round(a[1], 3)) for a in dir_cands}:
                    dir_cands.append(dv)
            best: tuple[float, float, float] | None = None  # (score, dx, dy)
            for dvx, dvy in dir_cands:
                for off in _PULL_OFFSETS:
                    tx, ty = _snap5(ax + dvx * off), _snap5(ay + dvy * off)
                    dx, dy = tx - mx, ty - my
                    nb = (mb[0] + dx, mb[1] + dy, mb[2] + dx, mb[3] + dy)
                    # 出图四边全查(2026-08-31 P6 目检:只查下/左漏了上沿——
                    # LED10 拉到锚脚上 190 落位后顶沿 876>813,clusters 出纸
                    # ERROR;带=clusters sheetUsable 同口径 [12,12,1158,813])。
                    # 试放虚空网格(x≥1500)本不在纸上,四边钳制只对「已在纸上」
                    # 的件生效,虚空件保持下/左地板判据(否则试放拉近全灭)。
                    if mb[0] <= 1158:
                        if nb[0] < 12 or nb[1] < 12 or nb[2] > 1158 or nb[3] > 813:
                            continue  # 出图
                    elif nb[0] < 12 or nb[1] < 12:
                        continue  # 虚空件只保地板
                    if any(
                        nb[0] - 15 < ob[2] and nb[2] + 15 > ob[0]
                        and nb[1] - 15 < ob[3] and nb[3] + 15 > ob[1]
                        for od, ob in boxes.items() if od != mover_d and ob
                    ):
                        continue  # 压他件
                    if self._move_touch_conflict(mover_d, dx, dy, emap):
                        continue  # 终位电气触点(共端点/T结):并网粘死,换候选
                    score = abs(dx) + abs(dy)
                    if best is None or score < best[0]:
                        best = (score, dx, dy)
            return (best[1], best[2]) if best else None

        pulls: list[dict] = []
        for net in sorted(nets):
            if len(pulls) >= _PULL_MAX:
                break
            pins = nets[net]
            if len(pins) != 2:
                continue  # 多脚网拉近一件修不了跨度,交线长门保 netport
            (d1, x1p, y1p), (d2, x2p, y2p) = pins
            if abs(x1p - x2p) + abs(y1p - y2p) <= _MAX_DIRECT_WIRE:
                continue
            c1, c2 = parts.get(d1) or {}, parts.get(d2) or {}
            n1, n2 = _pin_count(c1), _pin_count(c2)
            if n1 > _PULL_PIN_CAP and n2 > _PULL_PIN_CAP:
                continue  # 两件都大:谁也不该挪
            if n1 > _PULL_PIN_CAP >= n2:
                anchor_d, mover_d = d1, d2
            elif n2 > _PULL_PIN_CAP >= n1:
                anchor_d, mover_d = d2, d1
            else:
                # 双小件:脚多者为锚,平局按位号序(确定性)
                anchor_d, mover_d = (d1, d2) if (n1, d1) >= (n2, d2) else (d2, d1)
            ab, mb = boxes.get(anchor_d), boxes.get(mover_d)
            ac, mc = parts.get(anchor_d), parts.get(mover_d)
            if not (ab and mb and ac and mc):
                continue
            ax, ay = (x1p, y1p) if anchor_d == d1 else (x2p, y2p)
            mx, my = (x2p, y2p) if mover_d == d2 else (x1p, y1p)
            slot = _find_slot(ax, ay, ab, mb, mover_d, mx, my)
            if slot is None:
                continue  # 锚脚旁无空位:交线长门保 netport
            dx, dy = slot
            gid = self._isolate_designator(page, round_no, mover_d)
            if not gid:
                continue
            rc, _out, err = self.adapter.run(
                ["sch", "group-move", "--group", gid, "--dx", str(dx), "--dy", str(dy),
                 "--doc", page])
            via = "group-move" if rc == 0 else ""
            if rc != 0 and self._pull_modify_move(page, round_no, mover_d, mc, dx, dy,
                                                  avoid_pts=_avoid_pts()):
                # 真机 2026-08-27 定案:移动内核把纸边当硬墙,纸外虚空区的组
                # 一律拒移(试放带全在虚空 → 拉拢全灭);sch modify 走 SDK 属性
                # 通道改位不受钳(P8 废页实验 5000,5000→6200,5400 自由生效)。
                via = "modify"
                self.audit.event("pull-close-fallback", round_no=round_no, page=page,
                                 net=net, designator=mover_d, dx=dx, dy=dy,
                                 cause=(err or "")[-100:])
            if not via:
                self.audit.event("pull-close-fail", round_no=round_no, page=page,
                                 net=net, designator=mover_d, dx=dx, dy=dy,
                                 error=(err or "")[-150:])
                continue
            pulls.append({"net": net, "moved": mover_d, "dx": dx, "dy": dy, "via": via})
            # 本地几何同步平移:后续网的跨度/落点判断用新位
            _emap_translate(mover_d, dx, dy)
            boxes[mover_d] = (mb[0] + dx, mb[1] + dy, mb[2] + dx, mb[3] + dy)
            for lst in nets.values():
                for k, (dd, px, py) in enumerate(lst):
                    if dd == mover_d:
                        lst[k] = (dd, px + dx, py + dy)
            for p in (mc.get("pins") or []):
                if p.get("x") is not None:
                    p["x"], p["y"] = float(p["x"]) + dx, float(p["y"]) + dy
        # ── rail 孤件拉近(2026-08-28 run-fd3f51113bdc P7:ldo 块 4 件全 rail
        # ——3V3/5V/GND 无内部网名,直连/净距两通道都不触发,电容停在模板位
        # 离稳压器 250-480,块被摊成 426×561 上 oversize 页)。同块 ≥2 件时,
        # 全脚皆 rail/空网的件离锚件(脚最多者)最近脚距 > _RAIL_PULL_GAP 就
        # 拉到锚脚旁。试放相位同一入口——量测前拉近,cell 按收紧后体积定格,
        # 装箱页序直接受益;modify 回退只挪本体,挪完逐脚重落 rail 桩防孤儿。
        by_block: dict[str, list[str]] = {}
        for d in parts:
            if d in des_block:
                by_block.setdefault(des_block[d], []).append(d)
        rail_moved: list[tuple[str, dict]] = []
        for inst in sorted(by_block):
            if len(pulls) >= _PULL_MAX:
                break
            ds = [d for d in by_block[inst] if boxes.get(d)]
            if len(ds) < 2:
                continue  # 单件块无"伴生"可言(如去耦电容独立块)
            anchor = max(ds, key=lambda d: (_pin_count(parts[d]), d))
            ab, ac = boxes.get(anchor), parts.get(anchor)
            if not (ab and ac):
                continue
            a_pins = [(float(p["x"]), float(p["y"]))
                      for p in ac.get("pins") or [] if p.get("x") is not None]
            if not a_pins:
                continue
            for mover_d in sorted(d for d in ds if d != anchor):
                if len(pulls) >= _PULL_MAX:
                    break
                mc = parts.get(mover_d)
                mb = boxes.get(mover_d)
                if not (mc and mb):
                    continue
                ns = [str(p.get("net") or "") for p in mc.get("pins") or []]
                if not all(not n or self._is_rail_net(n) for n in ns):
                    continue  # 有非 rail 网:净距/直连通道管它
                m_pins = [(float(p["x"]), float(p["y"]))
                          for p in mc.get("pins") or [] if p.get("x") is not None]
                if not m_pins:
                    continue
                near = min(((abs(mx0 - ax0) + abs(my0 - ay0), ax0, ay0, mx0, my0)
                            for ax0, ay0 in a_pins for mx0, my0 in m_pins), default=None)
                if near is None or near[0] <= _RAIL_PULL_GAP:
                    continue
                _gd, ax, ay, mx, my = near
                slot = _find_slot(ax, ay, ab, mb, mover_d, mx, my)
                if slot is None:
                    continue
                dx, dy = slot
                gid = self._isolate_designator(page, round_no, mover_d)
                if not gid:
                    continue
                rc, _out, err = self.adapter.run(
                    ["sch", "group-move", "--group", gid, "--dx", str(dx), "--dy", str(dy),
                     "--doc", page])
                via = "group-move" if rc == 0 else ""
                if rc != 0:
                    # 纸外虚空(试放相位)group-move 必拒:退 modify 改位。重落桩
                    # (rail 桩被拆后按新位/原位回填)由 _pull_modify_move 内置,
                    # 不让接口标记变孤儿。
                    if not self._pull_modify_move(page, round_no, mover_d, mc, dx, dy,
                                                  avoid_pts=_avoid_pts()):
                        self.audit.event("pull-close-fail", round_no=round_no, page=page,
                                         net="(rail)", designator=mover_d, dx=dx, dy=dy,
                                         error=(err or "")[-150:])
                        continue
                    via = "modify"
                    self.audit.event("pull-close-fallback", round_no=round_no, page=page,
                                     net="(rail)", designator=mover_d, dx=dx, dy=dy,
                                     cause=(err or "")[-100:])
                pulls.append({"net": "(rail)", "moved": mover_d, "dx": dx, "dy": dy, "via": via})
                _emap_translate(mover_d, dx, dy)
                boxes[mover_d] = (mb[0] + dx, mb[1] + dy, mb[2] + dx, mb[3] + dy)
                for p in (mc.get("pins") or []):
                    if p.get("x") is not None:
                        p["x"], p["y"] = float(p["x"]) + dx, float(p["y"]) + dy
                rail_moved.append((mover_d, mc))
        # 拉移不保网,且丢网可以发生在整趟中途(run-0cdc61dd3eea P2:C1 拉
        # 移后 2 脚 GND 丢、同趟 C3 完好——共享 rail 网旗没跟着走;run-
        # 86f0ec3ab850 P2:C10 group-move 后自身即时回读仍带网,但后续邻居
        # C11 的 modify 回退 disconnect 拆共享 GND 网旗把 C10:2 拖成孤
        # 儿)。单件即时校验抓不到第二种形态——整趟结束统一回读全部 rail
        # 拉移件,按脚号比对拉前网(本地 mc 快照),丢了按新位重落
        # (_restub_net_pins);读失败按没丢处理,别让回读故障伤成二次重落。
        if rail_moved:
            try:
                comps_v, _ = self._list_components(page)
                cur_all = {str(c.get("designator")): {
                    str(p.get("pinNumber") or ""): str(p.get("net") or "")
                    for p in c.get("pins") or []}
                    for c in comps_v if c.get("componentType") == "part"}
            except Exception:  # noqa: BLE001
                cur_all = None
            for mover_d, mc in rail_moved:
                cur_nets = (cur_all or {}).get(mover_d)
                if cur_nets is None:
                    continue
                stubs_lost: list[tuple[str, float, float, str]] = []
                for p in mc.get("pins") or []:
                    pn = str(p.get("pinNumber") or "")
                    if (pn and p.get("net") and p.get("x") is not None
                            and not cur_nets.get(pn, "")):
                        stubs_lost.append((f"{mover_d}:{pn}", float(p["x"]),
                                           float(p["y"]), str(p["net"])))
                if stubs_lost:
                    self.audit.event("pull-netloss", round_no=round_no, page=page,
                                     designator=mover_d,
                                     pins=[s[0] for s in stubs_lost])
                    try:
                        self._restub_net_pins(page, round_no, mover_d, mc, stubs_lost,
                                              avoid_pts=_avoid_pts())
                    except Exception as e:  # noqa: BLE001
                        self.audit.event("pull-netloss-error", round_no=round_no,
                                         page=page, designator=mover_d,
                                         error=str(e)[:80])
        if pulls:
            self.audit.event("pull-close", round_no=round_no, page=page, pulls=pulls)
        return len(pulls)

    def _restub_net_pins(self, page: str, round_no: int, designator: str,
                         comp: dict,
                         stubs: list[tuple[str, float, float, str]],
                         avoid_pts: list[tuple[float, float]] | None = None) -> None:
        """重落刚拆的网桩(改位成功按新位/失败按原位,坐标由调用方给)。

        disconnect 清 net+删桩是平台真行为(run-fd3f51113bdc:拆线后
        pins.net='')——不回填,紧凑化按网名规划就永远看不见这些网,孤儿脚
        成永久断网(P4/P5 的"没连线"同族根因)。带内走 _connect_stub 确定性
        桩,出带(试放虚空)/失败退 planner autoconnect。connect/autoconnect
        作用于活动页,先翻页(紧凑化的翻页在本函数的调用点之后,此处自管)。"""
        if not stubs:
            return
        from edaloop.generate.adapter import AdapterError

        if not self._open_page_for_edit(page, "pull-restub-open", round_no):
            # 对抗评审:翻页失败=全部拆桩脚成永久孤儿,与 compact 恢复失败同
            # 性质断网,计入阈值门(不计数=该通道静默 fail-soft 复活)
            for ref, _x, _y, net in stubs:
                if net:
                    self._wire_breaks.append(f"{page}:{net}:{ref}:restub-open")
            return
        pp = [(float(p["x"]), float(p["y"])) for p in comp.get("pins") or []
              if p.get("x") is not None]
        # WIR-6(v0.6.11 审计 P0):调用方(_pull_modify_move 改位成功分支)给的
        # stubs 是平移后坐标,comp.pins 却还是拆桩时旧坐标(rail 路径的调用方
        # 自己 mutate 过 pins,modify 回退路径没有——不能在 _pull_modify_move
        # 里统一 mutate,否则 rail 路径双倍平移)。按 stub↔pin 脚号配对推
        # (dx,dy),把 pp 平移到件的真实新位——自件同行/同列短路检查查的才
        # 是真脚,不是留在原地的幽灵(真机形态:改位后新位正上/正下方有自件
        # 别脚,幽灵 pp 查不出,桩落上去=共端点并线短路)。
        _by_ref = {f"{designator}:{p.get('pinNumber')}": (float(p["x"]), float(p["y"]))
                   for p in comp.get("pins") or []
                   if p.get("pinNumber") and p.get("x") is not None}
        _shift = (0.0, 0.0)
        for ref, x, y, _net in stubs:
            q = _by_ref.get(str(ref))
            if q and (abs(q[0] - x) > 2.0 or abs(q[1] - y) > 2.0):
                _shift = (x - q[0], y - q[1])
                break
        if _shift != (0.0, 0.0):
            pp = [(px + _shift[0], py + _shift[1]) for px, py in pp]
        for ref, x, y, net in stubs:
            if not net:
                continue
            kind = ("gnd" if "GND" in str(net).upper()
                    else "power" if self._is_rail_net(net) else "netport")
            # 自件本体避让(他件上下文此热路径不读——残留压体由紧凑化后的
            # reseat 复扫收口,2026-08-31):方向=离带边最远曾把桩+标记甩进
            # 自件本体(P3 reverse_VIN 正中 REVERSER1)。
            ob = comp.get("bbox")
            own = None
            if isinstance(ob, dict) and "minX" in ob:
                own = (float(ob["minX"]), float(ob["minY"]),
                       float(ob["maxX"]), float(ob["maxY"]))
            r = self._connect_stub(ref, kind, net, x, y, pp,
                                   body_rects=[own] if own else None,
                                   own_body=own, avoid_pts=avoid_pts)
            if r is not None:
                if avoid_pts is not None:
                    d, off = r
                    avoid_pts.append((x + (off if d == "right" else -off if d == "left" else 0),
                                      y + (off if d == "up" else -off if d == "down" else 0)))
                continue
            try:
                rc, _o, _e = self.adapter.run(
                    ["sch", "autoconnect", "--pin", ref, "--kind", kind, "--net", net])
                if rc != 0:
                    self.audit.event("pull-restub-fail", round_no=round_no, page=page,
                                     pin=ref, net=net)
                    self._wire_breaks.append(f"{page}:{net}:{ref}:pull-restub")
            except AdapterError as e:
                self.audit.event("pull-restub-fail", round_no=round_no, page=page,
                                 pin=ref, net=net, error=str(e)[:80])
                self._wire_breaks.append(f"{page}:{net}:{ref}:pull-restub-ae")

    def _pull_modify_move(self, page: str, round_no: int, designator: str, comp: dict,
                          dx: float, dy: float,
                          avoid_pts: list[tuple[float, float]] | None = None) -> bool:
        """group-move 被拒(纸边钳制/拖线竞态)后的改位回退(P0-1 虚空区救援)。

        真机实验(2026-08-27,P8 废页):`sch modify --x/--y` 走 SDK 属性通道,
        不经移动内核的图纸边界钳制,纸外虚空区自由生效;但只移本体,桩线/
        netport 原地成孤儿(clusters 不计入任何组,bridge-check 会抓)。故先
        逐脚 disconnect 拆桩(删端点在该脚的全部导线+netport;rc≠0 容忍——
        桩可能已随此前别的删除带走),再改位;后续紧凑化轮按新几何重画直连
        线。modify 也失败 → 调用方记 pull-close-fail,线长门照旧保 netport。
        """
        from edaloop.generate.adapter import AdapterError

        pid = comp.get("primitiveId")
        ox, oy = comp.get("x"), comp.get("y")
        if not pid or ox is None or oy is None:
            return False
        stubs: list[tuple[str, float, float, str]] = []
        for p in comp.get("pins") or []:
            pn = str(p.get("pinNumber") or "")
            if pn and p.get("net") and p.get("x") is not None:
                stubs.append((f"{designator}:{pn}", float(p["x"]), float(p["y"]),
                              str(p["net"])))
                try:
                    self.adapter.run(["sch", "disconnect", "--pin", f"{designator}:{pn}",
                                      "--doc", page])
                except AdapterError:
                    pass  # 桩已不在此脚:残留交后续轮/bridge-check
        rc, _o, _e = self.adapter.run(
            ["sch", "modify", "--id", str(pid),
             "--x", f"{float(ox) + dx:.5g}", "--y", f"{float(oy) + dy:.5g}",
             "--doc", page])
        # 改位成功按新几何重落桩;失败按原位重落(回滚)——disconnect 连 net
        # 一起清(平台真行为),不回填则紧凑化看不见这些网,孤儿脚成永久断网
        moved = [(r, x + dx, y + dy, n) for r, x, y, n in stubs] if rc == 0 else stubs
        self._restub_net_pins(page, round_no, designator, comp, moved,
                              avoid_pts=avoid_pts)
        return rc == 0

    def _compact_internal_nets(self, page: str, round_no: int,
                               members: dict[str, list[str]]) -> None:
        """块内网紧凑化:内部网的 netport 对 → 直连导线(块框虚大的主因)。

        upstream block-apply 把所有网(含块内网)交给 autoconnect 走"脚桩+
        netport"(cmd_sch_block_apply_run.go),竖排 netport 文字长轴 86-96——
        P1 实测 59.5% 的框内是空白,led 块 90%+。凡"网名匹配 ^[A-Z]+\\d+_N\\d+$
        且 ≥2 脚全在同一块"的网:**先拆后画**——先 disconnect --pin 逐脚删
        脚桩+netport(此时本脚上只有桩线,--pin 的"删端点在该脚的全部导线"
        恰好只删桩),再 sch wire --net 同名并网;wire 失败用 autoconnect
        (--kind netport)把脚桩+netport 原样接回,不留断网。顺序不可反:
        真机 run-9e1c0a4e08d3 实测 EasyEDA"共端点即并线"(与角度无关),先画
        后拆会把直连线与桩合并成折返多段线,--flag-id 只查端点永远找不到桩。
        wire/disconnect/autoconnect 均无 --doc 钉扎(作用于活动页),先
        sch open --page 翻页。整体 fail-soft:任何异常只审计不改判。

        2026-08-26 补两道前置(P0-1/P1-6):读面走 `_list_components`(1MB
        stdout 截断降载重试);规划前先 `_pull_long_pairs` 把线距超限的
        2 脚对伴小件拉近锚脚——拉近成功的网才够格转直连线,拉不动的
        走线长门保 netport(见 `_MAX_DIRECT_WIRE`),不再无条件硬转。
        """
        # 自画线 bbox 记账(审计 P1):框=volume ∪ 自画线才覆盖块内全部墨迹。
        # 函数入口即清本页旧账——本页重入(试放相位→重放相位各调一次)以及
        # 早退(无候选/读失败)都不能留上一相位的幽灵框
        self._wire_boxes.pop(page, None)
        self._pull_long_pairs(page, round_no, members)
        try:
            comps, _deg = self._list_components(page)
        except Exception as e:  # noqa: BLE001
            self.audit.event("compact-nets", round_no=round_no, page=page,
                             error=f"list:{str(e)[:80]}")
            return
        des_block: dict[str, str] = {}
        for inst, desigs in members.items():
            for d in desigs:
                des_block[d] = inst
        parts = {c.get("designator"): c for c in comps
                 if c.get("componentType") == "part" and c.get("designator")}
        # 缺陷C 哨(2026-08-28 run-3ece9f39e1f2 P2/P8):紧凑化只该改几何不该改网。
        # 直连线端点落上他网墨迹时平台"共端点即并线",把 GND/5V/VIN 整网并成
        # 一根 multi-net wire(P2 全页零 GND、P8 三个网被 5V 吞)——脚网快照
        # 比对,漂移即审计 compact-netmerge;修复走人删桥线+按真值重接(该轮
        # P2 删 f722e556 后 C5:1→5V/C4:2→GND 重接、P8 清页重放块的实证)。
        net_snapshot = {str(c.get("designator")): {
            str(p.get("pinNumber") or ""): str(p.get("net") or "")
            for p in c.get("pins") or []}
            for c in comps if c.get("componentType") == "part"}
        marks = [c for c in comps if c.get("componentType") in ("netport", "netflag", "netlabel")]
        # 网收集(审计 P3 起):两类直连候选——①内部网(块编译器 ^[A-Z]+\d+_N\d+$,
        # 块内 netport 对纯属模板把式)②同页跨块信号网(≥2 不同块、非 rail:
        # 同页面对面还靠文字标签对接,目观就是"没连完")。rail(GND/电源族)不收:
        # 连接语义就是 marker,且 GND 连通一切,MST 会横穿整页。单脚网后续按
        # len<2 跳过。跨页安全:EasyEDA 同名网全局按名连通,他页 netport 不在本页
        # 操作面内,拆本页桩+画同名线不断链。
        nets: dict[str, list[tuple[str, str, float, float]]] = {}
        for d, c in parts.items():
            if d not in des_block:
                continue
            for p in c.get("pins") or []:
                n = str(p.get("net") or "")
                if n and (_INTERNAL_NET_RE.match(n) or not self._is_rail_net(n)):
                    nets.setdefault(n, []).append(
                        (d, str(p.get("pinNumber")), float(p["x"]), float(p["y"])))
        # 防撞素材:本体框 / 各脚点 / 各标记锚(逐对再排除本网元素)。
        # 降载读取(截断后去 --include-bbox)没有 bbox:退引脚凸包±10——
        # 本体框弱近似,穿框约束变松但脚/标记/并线三道硬约束仍在。
        bodies: dict[str, tuple[float, float, float, float]] = {}
        for d, c in parts.items():
            b = c.get("bbox")
            if isinstance(b, dict) and "minX" in b:
                bodies[d] = (float(b["minX"]), float(b["minY"]),
                             float(b["maxX"]), float(b["maxY"]))
            else:
                xs = [float(p["x"]) for p in (c.get("pins") or []) if p.get("x") is not None]
                ys = [float(p["y"]) for p in (c.get("pins") or []) if p.get("x") is not None]
                if xs:
                    bodies[d] = (min(xs) - 10, min(ys) - 10, max(xs) + 10, max(ys) + 10)
        pin_all = [(float(p["x"]), float(p["y"]), str(p.get("net")), d)
                   for d, c in parts.items() for p in (c.get("pins") or [])
                   if p.get("x") is not None]
        mark_rects = [(self._mark_rect(f), str(f.get("net") or f.get("name") or ""))
                      for f in marks if f.get("x") is not None]
        # 桩线段(脚↔本网标记,≤120——autoconnect 桩长 20/40/…/80 分道):
        # 并线障碍——他网的直连腿不能与之共线重叠
        stub_legs_by_net: dict[str, list[tuple]] = {}
        for f in marks:
            nf = str(f.get("net") or f.get("name") or "")
            if f.get("x") is None:
                continue
            fx, fy = float(f["x"]), float(f["y"])
            for px, py, pn, _pd in pin_all:
                if pn == nf and abs(px - fx) + abs(py - fy) <= 120.0:
                    stub_legs_by_net.setdefault(nf, []).append((px, py, fx, fy))
        converted: dict[str, str] = {}
        kept: dict[str, str] = {}
        # 不动点规划:第 1 轮按"障碍全保留"规划;此后每轮把"上一轮已可
        # 转换网"的桩线/标记从障碍中移除(它们随 disconnect 消失)再规划,
        # 直到转换集不动——真机 req-07 P1 实测有级联:C1_N4 直线可通 →
        # 其桩不再挡 C1_N9/C1_N12 的共线腿,4 轮收敛 23/25。5 轮不收敛
        # (转换集振荡)则退回零假设规划,保证画出的每条线都在"它规划时
        # 的障碍世界"里合法,不留下对幽灵桩的共线重叠。
        def _plan_round(removed: set[str]) -> list[tuple[str, list, list, list]]:
            planned_legs: list[tuple] = []  # 已排定直连腿:后续网布线的并线障碍
            out: list[tuple[str, list, list, list]] = []
            for net in sorted(nets):
                pins = nets[net]
                if len(pins) < 2:
                    continue  # 单脚网:netport 是唯一载体,不能拆
                blks = {des_block[d] for d, _p, _x, _y in pins}
                if None in blks:
                    kept[net] = "cross-block"
                    continue
                internal = bool(_INTERNAL_NET_RE.match(net))
                if internal and len(blks) != 1:
                    kept[net] = "cross-block"  # 内部名跨块:口径异常,不动
                    continue
                if not internal:
                    if len(blks) < 2:
                        # 单块外部网:远端在他页靠 netport(或本页对端未落),拆桩
                        # 收益零、netport 位置属块接口,不动
                        kept[net] = "solo-external"
                        continue
                    if self._is_rail_net(net):
                        kept[net] = "rail"
                        continue
                # 线长门(P0-1):曼哈顿跨度超限不转真线——块模板的 relational
                # 布局按 netport 标签连接设计,器距 500-900 是常态,硬转=横穿
                # 半图的长线(P1 J3:A5↔R11 875 / P2 J5:A5↔R49 510,2026-08-26
                # 定案)。对伴拉近已在规划前尝试过,仍超限=无空位,保留 netport
                # 交重落收紧;too-long 与 route-blocked 走同一重落兜底路径。
                # 跨块网 too-long 不重落(netport 位置属块间接口,审计 P3 定案)。
                span = max(
                    abs(pins[i][2] - pins[j][2]) + abs(pins[i][3] - pins[j][3])
                    for i in range(len(pins)) for j in range(i + 1, len(pins))
                )
                if span > _MAX_DIRECT_WIRE:
                    kept[net] = f"{'cross-' if not internal else ''}too-long({span:.0f})"
                    continue
                # 走线口径(2026-08-31 改):**全部本体设障,仅豁免本边两端
                # 器件**——旧口径把"网内成员块"整块本体豁免(理由"导线跨自家
                # 件图形是常规画法"),单块页(P7 电源块)于是整页无障,直连线
                # 全从器件本体上横穿(用户目检主诉"大部分线从器件上跨过去")。
                # 豁免只保留两端件:脚在本体渲染边沿上,出线段贴边沿几单位是
                # 常态,全设障会把一切出线段误杀。布不通的边照旧留 netport。
                # 成员块他网脚容差放宽 1.5(贴旁 2-3 单位无触点≠短路),他块脚 3
                fp = [(px, py) if des_block.get(pd) not in blks else (px, py, 1.5)
                      for px, py, pn, pd in pin_all if pn != net]
                fm = [r for r, mn in mark_rects
                      if mn != net and mn not in removed]
                wl = [l for n2, ls in stub_legs_by_net.items()
                      if n2 != net and n2 not in removed for l in ls]
                wl += planned_legs
                # 多脚网配对走 MST(P1-5):排序链在折返拓扑上产生 zig-zag 绕线
                # (C16_N4 四脚网,排序序≠邻接序),MST 总长是任何链方案下界。
                # 逐边降粒度(审计 P3):旧"任一边布不通→整网放弃"把 4 脚网里
                # 1 对堵死时的三条好边一起扔掉;现在好边照画,堵边两脚留
                # netport(autoconnect 回接)——部分改造严格优于不改造。
                routes: list[tuple[int, int, list]] = []
                for i, j in _mst_edges([(t[2], t[3]) for t in pins]):
                    a, b = pins[i], pins[j]
                    fb = [r for d, r in bodies.items() if d not in (a[0], b[0])]
                    rt = _route_pin_pair((a[2], a[3]), (b[2], b[3]), fb, fp, fm, wl)
                    if rt is None:
                        continue  # 堵边:留 netport,不定终身(下一轮可能解锁)
                    routes.append((i, j, rt))
                if not routes:
                    continue
                covered = {k for i, j, _rt in routes for k in (i, j)}
                spare = [t for k, t in enumerate(pins) if k not in covered]
                # 本网直连腿计入障碍(同网相接无害;堵边脚的桩保留——但粒度
                # 按网记,只有零堵边的全网才进 removed,见 fixedpoint)
                for _i, _j, rt in routes:
                    for u, v in zip(rt, rt[1:]):
                        planned_legs.append((u[0], u[1], v[0], v[1]))
                # pins 原序透传:routes 的 (i,j) 索引指它(拆脚/回接/spare 归因同源)
                out.append((net, routes, pins, spare))
            return out

        converting: set[str] = set()
        work = _plan_round(converting)
        for _plan_round_no in range(5):
            # removed 只收「零堵边」的全转换网:它们的桩/标记随 disconnect 全部
            # 消失;部分转换网的 spare 桩会回接复存,进 removed=给别网留幽灵空区
            new_conv = {net for net, _r, _o, sp in work if not sp}
            if new_conv == converting:
                break
            converting = new_conv
            work = _plan_round(converting)
        else:
            work = _plan_round(set())
        planned_all = {net for net, _r, _o, _sp in work}
        converting = {net for net, _r, _o, sp in work if not sp}
        for net in nets:
            if net in planned_all and net not in converting:
                # 部分转换:spare 脚刚回接默认桩,不能进 reseat(disconnect 会
                # 把已画的直连线一并删掉)——如实记录,spare 桩留待下轮
                sp = next(s for n2, _r, _o, s in work if n2 == net)
                kept[net] = f"partial(spare{len(sp)})"
            elif net not in converting and len(nets[net]) >= 2 and net not in kept:
                kept[net] = "route-blocked"
        # 无可转换网 ≠ 无活干:kept 里 route-blocked/too-long 的保留网仍要走
        # 紧贴重落(落-量-清改造后本函数按实例逐块调用,某块恰好全 too-long
        # 时 work 为空,旧早退会把重落一并跳过 → 量出的 vol 框虚大)。只有
        # 连重落候选也没有才真正空转早退。
        reseatable = [n for n, w in kept.items()
                      if w.startswith(("route-blocked", "too-long"))]
        if not work and not reseatable:
            self.audit.event("compact-nets", round_no=round_no, page=page,
                             converted=converted, kept=kept or {"none": "no-candidates"})
            return
        # wire/disconnect 都作用于活动页:先翻到目标页(--page 列表后会自动还原)
        try:
            _rc, out, _err = self.adapter.run(["sch", "pages"])
            pages = (json.loads(out) or {}).get("result", {}).get("pages") or []
            puuid = next((str(p.get("uuid")) for p in pages if p.get("name") == page), "")
            if puuid:
                self.adapter.run(["sch", "open", "--page", puuid])
            else:
                raise ValueError(f"page {page} uuid 未解析")
        except Exception as e:  # noqa: BLE001
            self.audit.event("compact-nets", round_no=round_no, page=page,
                             error=f"open:{str(e)[:80]}", converted=converted, kept=kept)
            return
        from edaloop.generate.adapter import AdapterError

        wire_boxes: list[tuple[str, str, tuple[float, float, float, float]]] = []
        for net, routes, pins, spare in work:
            # 1) 先拆:此时本脚上只有脚桩(本网直连线还不存在),--pin 的
            #    "删端点在该脚的全部导线"恰好只删桩+netport。rc≠0 容忍——
            #    桩可能已随此前别的删除并线带走,残留 netport 由同名并网兜住。
            cut = 0
            for d, p, _x, _y in pins:
                try:
                    rc, _o, _e = self.adapter.run(["sch", "disconnect", "--pin", f"{d}:{p}"])
                except AdapterError:
                    continue
                if rc == 0:
                    cut += 1
            # 2) 再画:同名并网,脚重新入网。任一段失败 → autoconnect 把全部
            #    脚的桩+netport 原样接回(电气等价于未动过),不留断网;回接
            #    也失败才计断网(审计 P0:恢复不看 rc=静默断网)。
            wired = True
            for _i, _j, rt in routes:
                try:
                    rc, _o, _e = self.adapter.run(
                        ["sch", "wire", "--points", json.dumps(rt), "--net", net])
                except AdapterError:
                    wired = False
                    break
                if rc != 0:
                    wired = False
                    break
            if not wired:
                for d, p, _x, _y in pins:
                    try:
                        rc, _o, _e = self.adapter.run(
                            ["sch", "autoconnect", "--pin", f"{d}:{p}",
                             "--kind", "netport", "--net", net])
                        if rc != 0:
                            self._wire_breaks.append(f"{page}:{net}:{d}:{p}:restore")
                    except AdapterError:
                        self._wire_breaks.append(f"{page}:{net}:{d}:{p}:restore-ae")
                kept[net] = f"wire-failed(cut{cut},reconnected)"
                continue
            # 2b) 堵边 spare 脚回接 netport(逐边降粒度:堵边的两端保标签语义;
            #     rc≠0 即断网,如实计数不再 fail-soft 吞掉)
            for d, p, _x, _y in spare:
                try:
                    rc, _o, _e = self.adapter.run(
                        ["sch", "autoconnect", "--pin", f"{d}:{p}",
                         "--kind", "netport", "--net", net])
                    if rc != 0:
                        self._wire_breaks.append(f"{page}:{net}:{d}:{p}:spare")
                except AdapterError:
                    self._wire_breaks.append(f"{page}:{net}:{d}:{p}:spare-ae")
            converted[net] = f"{len(pins)}p/cut{cut}/spare{len(spare)}"
            # 成功画出的线记 bbox(归因两端块:框要罩住自画墨迹;跨块线两端
            # 块的框都并——框交叠是"墨迹相连"的诚实呈现)
            for i, j, rt in routes:
                xs = [pt[0] for pt in rt]
                ys = [pt[1] for pt in rt]
                ia = des_block.get(pins[i][0]) or ""
                ib = des_block.get(pins[j][0]) or ""
                wire_boxes.append((ia, ib,
                                   (min(xs) - 3, min(ys) - 3, max(xs) + 3, max(ys) + 3)))
        # 3) 保留网紧贴重落:route-blocked / too-long 的同块内部网保住 netport
        #    语义,但 block-apply 的 spec 为逃块内拥挤把桩上限放得很高(真机
        #    run-09fcc8639b15 J2.A7 实测 265 长桩,与他网直连线仍构成 10
        #    间距长平行)。直连线画完后几何已变,拆掉重落一次收紧到 40 内;
        #    失败退回默认上限再落,不留断网。cross-block 保留网不动——
        #    它们的 netport 是块间连接语义,位置属于块接口。
        for net, why in list(kept.items()):
            if not why.startswith(("route-blocked", "too-long")):
                continue
            reseated = 0
            for d, p, _x, _y in nets[net]:
                try:
                    self.adapter.run(["sch", "disconnect", "--pin", f"{d}:{p}"])
                    rc, _o, _e = self.adapter.run(
                        ["sch", "autoconnect", "--pin", f"{d}:{p}", "--kind", "netport",
                         "--net", net, "--offset-max", "40"])
                    if rc != 0:
                        rc2, _o2, _e2 = self.adapter.run(
                            ["sch", "autoconnect", "--pin", f"{d}:{p}", "--kind",
                             "netport", "--net", net])
                        if rc2 != 0:
                            # 拆桩成功+两档重落全失败=该脚无载体,真断网:
                            # 计数交 run() 阈值阻断(审计 P0,不再 fail-soft 吞掉)
                            self._wire_breaks.append(f"{page}:{net}:{d}:{p}:reseat")
                    reseated += 1
                except AdapterError:
                    self._wire_breaks.append(f"{page}:{net}:{d}:{p}:reseat-ae")
                    continue  # 与转换失败同口径:gate 的 sch nets 抓断网
            if reseated:
                kept[net] = f"{why.split('(', 1)[0]}(reseated{reseated})"
        # 缺陷C 哨比对:终读全部件,快照网名漂移(并入他网 or 丢网)即审计。
        # 回读失败按"没漂移"处理(fail-soft,与整趟紧凑化同口径)。
        try:
            comps_v, _ = self._list_components(page)
            after = {str(c.get("designator")): {
                str(p.get("pinNumber") or ""): str(p.get("net") or "")
                for p in c.get("pins") or []}
                for c in comps_v if c.get("componentType") == "part"}
            drifted = [f"{d}:{pn}:{n0}->{n1 or '(空)'}"
                       for d, pnmap in net_snapshot.items() if d in after
                       for pn, n0 in pnmap.items()
                       if after[d].get(pn, "") != n0]
            if drifted:
                self.audit.event("compact-netmerge", round_no=round_no, page=page,
                                 pins=drifted[:20])
        except Exception:  # noqa: BLE001
            pass
        self._wire_boxes[page] = wire_boxes
        self.audit.event("compact-nets", round_no=round_no, page=page,
                         converted=converted, kept=kept)

    # ── freeze-pack 目检质量两关(run-fc264cf3ac76 目检反馈,均在紧凑化之前)──

    @staticmethod
    def _is_rail_net(net: str) -> bool:
        """轨道网(GND/电源族):连接语义靠各自的 marker,同网他件脚不是走线伙伴。"""
        n = str(net or "").strip().upper()
        return (n in {"GND", "AGND", "PGND", "DGND", "VCC", "VDD", "VEE", "VIN",
                      "VOUT", "VBUS", "VBAT", "3V3", "5V", "1V8", "2V5", "+3V3", "+5V"}
                or "GND" in n)

    @staticmethod
    def _mark_kind(component_type: str, net: str) -> str:
        """重落用的 autoconnect kind:从被拆 marker 的原类型/net 推,不引入新语义。"""
        if component_type == "netflag":
            return "gnd" if "GND" in str(net or "").upper() else "power"
        return "netport"

    @staticmethod
    def _mark_span(net: str, kind: str) -> float:
        """marker 文字翼展(顺桩方向从锚点继续延伸):netport 横排文字 ≈12/字符
        (run-885b01f68b1f 实测:ST2_IN2 7 字 80 宽、STEP2_C 桩右伸 90、
        LED_FAULT 9 字 92),netflag 符号+短文字 40;下限 60 兜短名。
        sch list 的 mark bbox 只是符号箭头,整段文字要在 clusters 才量得到,
        所以这里只能按字长估。"""
        if kind == "netport":
            return max(60.0, 12 * len(str(net or "")) + 10)
        return 40.0

    def _mark_rect(self, f: dict) -> tuple[float, float, float, float]:
        """marker 墨迹矩形(v0.6.11 审计 P3:走线障碍从锚点容差 5 改矩形)。

        sch list 回的 mark bbox 只包符号箭头,netport 整段横排文字量不到,
        按锚点横向 ±span/2、纵向 ±14 估;有 bbox 就并集(符号可能偏在锚点
        一侧)。_leg_hits_rect 自带 pad 2,这里不再外扩。"""
        kind = "netflag" if f.get("componentType") == "netflag" else "netport"
        span = self._mark_span(str(f.get("net") or f.get("name") or ""), kind)
        mx, my = float(f["x"]), float(f["y"])
        x1, y1, x2, y2 = mx - span / 2.0, my - 14.0, mx + span / 2.0, my + 14.0
        b = f.get("bbox")
        if isinstance(b, dict) and "minX" in b:
            x1, y1 = min(x1, float(b["minX"])), min(y1, float(b["minY"]))
            x2, y2 = max(x2, float(b["maxX"])), max(y2, float(b["maxY"]))
        return (x1, y1, x2, y2)

    def _connect_stub(self, pin_ref: str, kind: str, net: str,
                      px: float, py: float,
                      part_pins: list[tuple[float, float]],
                      body_rects: list[tuple[float, float, float, float]] | None = None,
                      own_body: tuple[float, float, float, float] | None = None,
                      avoid_pts: list[tuple[float, float]] | None = None,
                      ) -> tuple[str, int] | None:
        """确定性落桩(sch connect 显式方向+桩长,绕开 planner)。

        为什么不走 spec:v1.1.1 的 acSpecRules 只认 offsetRange/offsetStep,
        offsetCap/offsetMin/offsetMax 键被 Go 反序列化静默丢弃(run-fd4bb14b4ee6:
        SWDHDR1:4 传 cap 40 实落 down/54,正是默认细档 18+6×6);planner 又无
        方向控制,down 侧永远最便宜。本方法自算几何:方向按**引脚所在侧的
        外侧优先**排(2026-09-01 用户规范「连线不得穿越器件本体」,run-
        7db9b9f61430 目检:AMS1117 pin2 在左缘而 3V3 旗钉本体正中、STM32
        左排 7/8/9/10 脚的网全落右排——「离带边最远」与引脚侧无关,天然
        产出穿体桩),桩长档 30/60/90(+避体续 120/150/210/270/330,2026-09-01
        布局治本扩档:重跑#2 盲退 fallback 94-110 枚/run 的主因是长档缺位——
        短中档墨迹擦邻件、长档一出即净;带内界检仍是硬界不放松)里取「锚点 +
        文字翼展(_mark_span,顺方向延伸)」仍在带内、桩线/标记墨迹不压任何
        本体框的首个候选;
        自件同列/同行脚落在桩线段内 = 短路,换下一候选。
        body_rects(2026-08-31):要避让的本体框(调用方给自件+他件;None=
        旧口径不查)。此前只查带边+自件脚——「方向=离带边最远」会把标记甩
        进自件本体(P3 reverse_VIN 落 REVERSER1 正中 (195,110)、P5 U3 脚4
        GND 旗压 U3 本体都是此形)。全部候选都压体时取压叠面积最小的候选
        (仍优于 planner 盲落,且确定性);桩线段查本体时框内缩 3,防「脚在
        本体渲染边内几单位、外伸段被 pad 误杀」。own_body(同日补):自件
        框只进墨迹检查、**不进桩线检查**——脚长在自件本体边缘上,桩从脚
        出发必然穿自件本体(几何强制),按桩线查自件=全候选覆没退 planner
        (reseat 实测 J1:2 fallback)。桩跨自件图形是常规画法,墨迹压自件
        才是 P3 病。avoid_pts(2026-09-01,run-30c3833705a4 P4 定性):电气
        端点集(他件脚端点+他标记锚点+本轮已落锚点)——候选锚点与之重合
        (≤4)或桩线段贴过它(≤2.5)= 端点重合/T 结 = **并网**(P4 GND 旗
        与 C7_N4 netport 同锚 (910,460) → 电源网 isGlobal 一点并轨,GND↔5V
        全局短接、四页 GND 齐灭,且并网后网对象粘死重落也修不回);目标脚
        自身(±2)不算障碍。AdapterError = 环境错,直接 None 让调用方退
        planner 保连通。"""
        from edaloop.generate import packer
        from edaloop.generate.adapter import AdapterError

        bx1, by1, bx2, by2 = packer.BAND
        span = self._mark_span(net, kind)  # 文字翼展,顺桩方向延伸
        # 目标脚自己(及同点脚=同电节点)不算障碍:桩线从它出发
        part_pins = [q for q in part_pins if abs(q[0] - px) + abs(q[1] - py) > 2]
        # 方向序:引脚外侧优先。脚对 own_body 四边哪条最近(≤20,含引线外伸
        # 几单位)即该边为外侧——符号脚都长在本体边上;外侧 → 两侧顺边(桩线
        # 贴边平行不穿体,按带边空间排先后)→ **对侧垫底**:脚在边上,对侧桩
        # 的桩线必横穿本体,只有外侧+顺边全灭时才轮到它(仍优于 planner 盲落
        # 把标记钉进本体——(905,740) 钉 AMS1117 正中即盲落所为)。脚不贴边
        # (热盘/中心脚/无 own_body)退旧序:带边空间降序。
        room = {"up": by2 - py, "down": py - by1,
                "left": px - bx1, "right": bx2 - px}
        order = sorted(room, key=lambda dd: -room[dd])
        if own_body:
            rx1, ry1, rx2, ry2 = own_body
            gap = {"left": px - rx1, "right": rx2 - px,
                   "up": ry2 - py, "down": py - ry1}
            side = min(gap, key=lambda dd: gap[dd])
            if gap[side] <= 20.0:
                perp = ("up", "down") if side in ("left", "right") else ("left", "right")
                order = [side, *sorted(perp, key=lambda dd: -room[dd]),
                         {"left": "right", "right": "left",
                          "up": "down", "down": "up"}[side]]
        offsets = (30, 60, 90) if body_rects is None \
            else (30, 60, 90, 120, 150, 210, 270, 330)
        fallback_best: tuple[float, tuple[str, int]] | None = None  # (压叠面积, 候选)
        for d in order:
            for off in offsets:
                ax, ay = px, py
                if d == "up":
                    ay, ix, iy = py + off, px, py + off + span
                elif d == "down":
                    ay, ix, iy = py - off, px, py - off - span
                elif d == "left":
                    ax, ix, iy = px - off, px - off - span, py
                else:
                    ax, ix, iy = px + off, px + off + span, py
                if not (bx1 - 2 <= min(ax, ix) and max(ax, ix) <= bx2 + 2
                        and by1 - 2 <= min(ay, iy) and max(ay, iy) <= by2 + 2):
                    continue  # 锚点或文字翼展出带
                if avoid_pts and any(
                    (abs(qx - ax) <= 4.0 and abs(qy - ay) <= 4.0)
                    or _leg_near_point(px, py, ax, ay, qx, qy, 2.5)
                    for (qx, qy) in avoid_pts
                    if abs(qx - px) + abs(qy - py) > 2
                ):
                    continue  # 锚点重合/桩线贴过电气端点 = 端点并网,换候选
                blocked = False
                for (qx, qy) in part_pins:
                    if d in ("up", "down") and abs(qx - px) <= 2 \
                            and min(py, ay) - 2 <= qy <= max(py, ay) + 2:
                        blocked = True
                        break
                    if d in ("left", "right") and abs(qy - py) <= 2 \
                            and min(px, ax) - 2 <= qx <= max(px, ax) + 2:
                        blocked = True
                        break
                if blocked:
                    continue
                if body_rects:
                    # 标记墨迹矩形:锚点↔翼端连线外包络,横向 ±14(_mark_rect 同口径)
                    mx1, mx2 = sorted((ax, ix))
                    my1, my2 = sorted((ay, iy))
                    mrect = (mx1, my1 - 14.0, mx2, my2 + 14.0)
                    # 桩线段(脚→锚点)内缩 3 查:脚在本体渲染边内几单位是常态,
                    # 不内缩会把一切外伸桩全误杀
                    leg_ok = True
                    overlap = 0.0
                    for (rx1, ry1, rx2, ry2) in body_rects:
                        if (rx1, ry1, rx2, ry2) != own_body \
                                and _leg_hits_rect(px, py, ax, ay,
                                                   (rx1 + 3, ry1 + 3, rx2 - 3, ry2 - 3)):
                            leg_ok = False
                            break
                        ow = min(mrect[2], rx2) - max(mrect[0], rx1)
                        oh = min(mrect[3], ry2) - max(mrect[1], ry1)
                        if ow > 2 and oh > 2:
                            overlap += ow * oh
                    if not leg_ok:
                        continue
                    if overlap > 0.0:
                        if fallback_best is None or overlap < fallback_best[0]:
                            fallback_best = (overlap, (d, off))
                        continue
                try:
                    rc, _o, _e = self.adapter.run(
                        ["sch", "connect", "--pin", pin_ref, "--kind", kind,
                         "--net", net, "--direction", d, "--offset", str(off)])
                except AdapterError:
                    return None
                if rc == 0:
                    return (d, off)
        if body_rects and fallback_best is not None:
            d, off = fallback_best[1]
            try:
                rc, _o, _e = self.adapter.run(
                    ["sch", "connect", "--pin", pin_ref, "--kind", kind,
                     "--net", net, "--direction", d, "--offset", str(off)])
            except AdapterError:
                return None
            if rc == 0:
                return (d, off)
        return None

    def _guarded_autoconnect(self, page: str, round_no: int, ref: str, kind: str,
                             net: str, px: float, py: float,
                             opins: list[tuple[float, float]],
                             own_body, body_rects, avoid_pts,
                             tag: str) -> str:
        """planner 盲退的几何质量关(2026-09-01 布局治本批,重跑#2 翼擦 12 根因)。

        autoconnect 盲落无任何几何检查(#1/#2 fallback 规模 94/110 枚/run,
        reprobe 在末轮 reseat 之前跑——盲落标记在探针后落纸,末端几何无人看)。
        流程:落前/落后各列一次页几何,新旧锚点差集定位**本脚新标记**(fake 的
        autoconnect 不落标记 → 差集空,按原盲退放行,老测试零扰动);新标记
        墨迹压本体/压他件标记/出带 = 盲落质量事故 → 拆脚重走 _connect_stub
        (此刻页几何是新的:同轮前序重落/扩避让档 210→330 让首轮失败的候选
        此刻可成;avoid_pts 由调用方维护 live 追加);再失败重退 planner 盲落
        保连通(宁翼擦不隐短),全程入审计——翼擦从「静默」变「计数」,对齐
        §10 软指标记账口径。返回:"error"(autoconnect 失败,调用方按原断网
        口径计数)/"blind"(无新标记可检或落点合格)/"reguard"(盲落事故已
        拆+确定性重落)/"unguarded"语义并入 "blind"(审计里带 outcome 区分)。"""
        from edaloop.generate import packer
        from edaloop.generate.adapter import AdapterError

        def _geom():
            try:
                comps, _deg = self._list_components(page)
            except Exception:  # noqa: BLE001
                return None, None, None
            marks = [f for f in comps
                     if f.get("componentType") in ("netport", "netflag", "netlabel")
                     and f.get("x") is not None]
            bodies = []
            for c in comps:
                if c.get("componentType") != "part" or not c.get("designator"):
                    continue
                b = c.get("bbox")
                if isinstance(b, dict) and "minX" in b:
                    bodies.append((float(b["minX"]), float(b["minY"]),
                                   float(b["maxX"]), float(b["maxY"])))
            anchors = {(round(float(f["x"]), 1), round(float(f["y"]), 1))
                       for f in marks}
            return marks, bodies, anchors

        _marks0, _bodies0, anchors0 = _geom()
        try:
            rc, _o, _e = self.adapter.run(
                ["sch", "autoconnect", "--pin", ref, "--kind", kind, "--net", net])
        except AdapterError:
            return "error"
        if rc != 0:
            return "error"
        if anchors0 is None:
            return "blind"  # 落前几何读不到,无从质检,按原盲退
        marks1, bodies1, _anchors1 = _geom()
        if not marks1:
            return "blind"
        bx1, by1, bx2, by2 = packer.BAND
        bad = None
        for f in marks1:
            if str(f.get("net") or f.get("name") or "") != net:
                continue
            mx, my = float(f["x"]), float(f["y"])
            if (round(mx, 1), round(my, 1)) in anchors0:
                continue  # 旧标记,不是本次盲落
            mrect = self._mark_rect(f)
            if (mx < bx1 - 5 or mx > bx2 + 5 or my < by1 - 5 or my > by2 + 5
                    or any(mrect[0] < rx2 - 2 and mrect[2] > rx1 + 2
                           and mrect[1] < ry2 - 2 and mrect[3] > ry1 + 2
                           for (rx1, ry1, rx2, ry2) in bodies1)
                    or any(mrect[0] < g2[2] - 2 and mrect[2] > g2[0] + 2
                           and mrect[1] < g2[3] - 2 and mrect[3] > g2[1] + 2
                           for g in marks1
                           if g is not f
                           for g2 in (self._mark_rect(g),))):
                bad = (f, mx, my)
                break
        if bad is None:
            return "blind"
        f, mx, my = bad
        try:
            self.adapter.run(["sch", "disconnect", "--pin", ref])
            avoid_r = list(bodies1 or body_rects or [])
            avoid_r += [self._mark_rect(g) for g in marks1 if g is not f]
            r = self._connect_stub(ref, kind, net, px, py, opins,
                                   body_rects=avoid_r or None,
                                   own_body=own_body, avoid_pts=avoid_pts)
        except AdapterError:
            r = None
        if r is not None:
            if avoid_pts is not None:
                nax = px + (r[1] if r[0] == "right" else -r[1] if r[0] == "left" else 0)
                nay = py + (r[1] if r[0] == "up" else -r[1] if r[0] == "down" else 0)
                avoid_pts.append((nax, nay))
            self.audit.event(tag, round_no=round_no, page=page, pin=ref, net=net,
                             bad_drop=f"{mx:.0f},{my:.0f}",
                             outcome="reguard", reseat=f"{r[0]}/{r[1]}")
            return "reguard"
        try:
            rc2, _o2, _e2 = self.adapter.run(
                ["sch", "autoconnect", "--pin", ref, "--kind", kind, "--net", net])
        except AdapterError:
            rc2 = 1
        self.audit.event(tag, round_no=round_no, page=page, pin=ref, net=net,
                         bad_drop=f"{mx:.0f},{my:.0f}",
                         outcome="unguarded" if rc2 == 0 else "error")
        return "blind" if rc2 == 0 else "error"

    def _open_page_for_edit(self, page: str, tag: str, round_no: int) -> bool:
        """disconnect/autoconnect/spec 都作用于活动页:先翻到目标页(同紧凑化)。"""
        try:
            _rc, out, _err = self.adapter.run(["sch", "pages"])
            pages = (json.loads(out) or {}).get("result", {}).get("pages") or []
            puuid = next((str(p.get("uuid")) for p in pages if p.get("name") == page), "")
            if not puuid:
                return False
            self.adapter.run(["sch", "open", "--page", puuid])
            return True
        except Exception as e:  # noqa: BLE001
            self.audit.event(tag, round_no=round_no, page=page, error=f"open:{str(e)[:80]}")
            return False

    def _rotate_outward_pins(self, page: str, round_no: int) -> None:
        """对脚旋转(run-fc264cf3ac76 目检:led_fault 的 LED12_N2 走 U 形 200,
        旋转 180° 后直连只需 80)。

        2 脚共线件,某脚的桩向"背离同网伙伴"方向出线(autoconnect 逐脚规划,
        不知道对端在哪)——180° 刚性旋转让两脚互换、伙伴回到同侧,紧凑化即可
        画直线。判据保守:触发脚的网非轨道且页内有同网他件脚(伙伴),桩方向与
        伙伴方向点积 < -0.3,且对脚位置在伙伴侧;另一脚若也有伙伴,要求旋转不
        把它从伙伴侧挪到背离侧(轨道网 marker 随脚重落,天然无约束)。电气保真:
        刚性旋转 pin1 仍是 pin1、极性不变,绝不镜像;modify 后不信回包(平台
        回显族),重列 pins 验证互换成立才重连。fail-soft:任何失败只审计。
        """
        from edaloop.generate.adapter import AdapterError

        try:
            comps, _deg = self._list_components(page)
            parts = {c["designator"]: c for c in comps
                     if c.get("componentType") == "part" and c.get("designator")}
            if not parts:
                return
        except Exception as e:  # noqa: BLE001
            self.audit.event("freeze-pack-rotate", round_no=round_no, page=page,
                             error=f"list:{str(e)[:80]}")
            return
        if not self._open_page_for_edit(page, "freeze-pack-rotate", round_no):
            return
        rotated: list[dict] = []
        failed: list[str] = []

        def _pins_of(comp, need_net: bool = True) -> list[tuple[str, float, float, str]]:
            outp = []
            for p in comp.get("pins") or []:
                n = str(p.get("net") or "")
                if (n or not need_net) and p.get("x") is not None:
                    outp.append((str(p.get("pinNumber")), float(p["x"]), float(p["y"]), n))
            return outp

        def _partner_centroid(net: str, desig: str):
            px = py = 0.0
            cnt = 0
            for d2, c2 in parts.items():
                if d2 == desig:
                    continue
                for _pn, x, y, n2 in _pins_of(c2):
                    if n2 == net:
                        px += x
                        py += y
                        cnt += 1
            return (px / cnt, py / cnt) if cnt else None

        for desig, comp in list(parts.items()):
            pins = _pins_of(comp)
            if len(pins) != 2 or abs(pins[0][1] - pins[1][1]) > 2 and abs(pins[0][2] - pins[1][2]) > 2:
                continue  # 只处理 2 脚共线件(横列或竖列)
            (pa, qa) = pins

            def _flip_gain(p, q):
                """p 的桩背离伙伴且 q 在伙伴侧 → 旋转收益;None=不触发。"""
                n = p[3]
                if self._is_rail_net(n):
                    return None
                cen = _partner_centroid(n, desig)
                if cen is None:
                    return None
                # 桩方向 = 同网最近 marker 相对脚的方向
                best, bd = None, 1e18
                for f in comps:
                    if f.get("componentType") not in ("netport", "netflag", "netlabel"):
                        continue
                    if str(f.get("net") or f.get("name") or "") != n or f.get("x") is None:
                        continue
                    dd = abs(float(f["x"]) - p[1]) + abs(float(f["y"]) - p[2])
                    if dd < bd:
                        best, bd = f, dd
                if best is None:
                    return None
                ux, uy = float(best["x"]) - p[1], float(best["y"]) - p[2]
                L = (ux * ux + uy * uy) ** 0.5
                if L < 1:
                    return None
                vx, vy = cen[0] - p[1], cen[1] - p[2]
                V = (vx * vx + vy * vy) ** 0.5
                if V < 1:
                    return None
                # 触发 = 桩不朝向伙伴(背离 dot<-0.3 或垂直 dot≤0.3 都算)。
                # 垂直桩是模板常态(桩垂直于脚排行、伙伴在体另一侧),不旋则
                # 直连线从伙伴穿本体到远端脚(run-fd3f51113bdc:LED4/6/7 线绕)。
                if (ux * vx + uy * vy) / (L * V) > 0.3:
                    return None  # 桩已朝向伙伴:旋了也白旋
                wx, wy = q[1] - p[1], q[2] - p[2]
                W = (wx * wx + wy * wy) ** 0.5
                if W < 1 or (vx * wx + vy * wy) / (V * W) < 0.3:
                    return None  # 对脚不在伙伴侧,旋了也白旋
                return f"{n}"

            gain = _flip_gain(pa, qa) or _flip_gain(qa, pa)
            if not gain:
                continue
            # 另一脚若也有伙伴:不许把它从伙伴侧挪到背离侧。带量级门——脚距
            # 增量小于 max(40, 20%原距) 算噪声(远端接口件挪 40 不构成伤害),
            # 否则 LED 型旋转会被页对面同名网脚一票否决(run-fd3f51113bdc
            # P4:M1_A 的 ULN 侧脚离 600+,LED4 旋后 +40 就被 hurts 拦死)。
            def _hurts(p, q):
                n = p[3]
                if self._is_rail_net(n):
                    return False
                cen = _partner_centroid(n, desig)
                if cen is None:
                    return False
                vx, vy = cen[0] - q[1], cen[1] - q[2]  # 旋后 p 落在 q 位
                V = (vx * vx + vy * vy) ** 0.5
                ux, uy = cen[0] - p[1], cen[1] - p[2]  # 旋前 p 位
                U = (ux * ux + uy * uy) ** 0.5
                if V < 1 or U < 1:
                    return False
                return V > U + max(40.0, 0.2 * U)  # 显著离伙伴更远才算伤
            if _hurts(pa, qa) or _hurts(qa, pa):
                continue
            marks = [f for f in comps if f.get("componentType") in ("netport", "netflag", "netlabel")
                     and str(f.get("net") or f.get("name") or "") in (pa[3], qa[3])]
            try:
                reconnected = False  # 拆桩后未完成重连的异常=断网;读刷新异常不算
                for p in (pa, qa):
                    self.adapter.run(["sch", "disconnect", "--pin", f"{desig}:{p[0]}"])
                self.adapter.run(["sch", "modify", "--id", str(comp.get("primitiveId") or ""),
                                  "--rotation", "180"])
                # 回包不可信(回显族):重列验证两脚确已互换
                _rc, out2, _e2 = self.adapter.run(
                    ["sch", "list", "--page", page, "--include-pins", "--include-bbox"])
                rep2 = json.loads(out2) if (out2 or "").strip() else {}
                comp2 = next((c for c in (rep2.get("result", {}).get("components") or [])
                              if c.get("designator") == desig), None)
                # 按脚号认脚且容空网:disconnect 已把两脚的网清空(真机
                # run-fd3f51113bdc:LED5/8-11、C5/C6 拆线后 pins.net=''),
                # 按网找脚永远 None → 既不验证也不重连,12 件裸奔;fake 曾
                # 让 net 粘住故测试全绿(fake-reality 分歧,同 2026-08-25 #5)
                pins2 = _pins_of(comp2, need_net=False) if comp2 else []
                # 验证口径:pa 脚号现在必须落在旧 qa 位(真互换,而非没转成)
                p2a = next((p for p in pins2 if p[0] == pa[0]), None)
                ok = p2a is not None and abs(p2a[1] - qa[1]) + abs(p2a[2] - qa[2]) <= 6
                # 无论旋转是否验证通过,两脚都已拆——重连必须发生,不留断网。
                # 旋后现行坐标以 pins2 为准,网用拆前原网(p[3])。
                stubs: list[tuple[str, int] | str] = []
                all_ok = True
                for p in (pa, qa):
                    cur = next((pp for pp in pins2 if pp[0] == p[0]), None)
                    kind = self._mark_kind(
                        str(next((m.get("componentType") for m in marks
                                  if str(m.get("net") or m.get("name") or "") == p[3]),
                            "netport")), p[3])
                    if cur is None:
                        all_ok = False
                        continue
                    r = self._connect_stub(f"{desig}:{cur[0]}", kind, p[3], cur[1], cur[2],
                                           [(pp[1], pp[2]) for pp in pins2])
                    if r is not None:
                        stubs.append(r)
                        continue
                    try:  # 保连通优先:退 planner 逐脚落(无方向控制,gate 抓断网)
                        rc3, _o3, _e3 = self.adapter.run(
                            ["sch", "autoconnect", "--pin", f"{desig}:{cur[0]}",
                             "--kind", kind, "--net", p[3]])
                    except AdapterError:
                        rc3 = 1
                    stubs.append("auto" if rc3 == 0 else "none")
                    if rc3 != 0:
                        all_ok = False
                        # 两脚已拆,重连失败=断网(计数入阈值门;freeze/生产同判)
                        self._wire_breaks.append(
                            f"{page}:{p[3]}:{desig}:{cur[0]}:rotate-restub")
                reconnected = True  # 重连循环走完(含 autoconnect 兜底),此后异常=读刷新
                if not ok:
                    failed.append(f"{desig}:rotation-not-verified(stubs={stubs})")
                    continue
                if all_ok:
                    rotated.append({"part": desig, "net": gain, "stubs": stubs,
                                    "pins": [f"{p[0]}@{p[1]:.0f},{p[2]:.0f}" for p in (pa, qa)]})
                else:
                    failed.append(f"{desig}:reconnect-failed(stubs={stubs})")
                # 几何已变:重列刷新 marks/伙伴数据再找下一个
                rc, out, _err = self.adapter.run(
                    ["sch", "list", "--page", page, "--include-pins", "--include-bbox"])
                rep = json.loads(out) if (out or "").strip() else {}
                comps = rep.get("result", {}).get("components") or []
                parts = {c["designator"]: c for c in comps
                         if c.get("componentType") == "part" and c.get("designator")}
            except AdapterError as e:
                failed.append(f"{desig}:{str(e)[:50]}")
                if not reconnected:  # 拆桩后异常逃逸:两脚裸奔,计断网
                    for _p in (pa, qa):
                        if _p[3]:
                            self._wire_breaks.append(
                                f"{page}:{_p[3]}:{desig}:{_p[0]}:rotate-ae")
        if rotated or failed:
            self.audit.event("freeze-pack-rotate", round_no=round_no, page=page,
                             rotated=rotated, failed=failed)

    def _reseat_escape_marks(self, page: str, round_no: int) -> None:
        """越带桩重落(run-fc264cf3ac76 目检:uln_st2 的 GND/VIN 密集区兜底桩
        垂到 y=-26——clusters 判 out-of-sheet ERROR,块框 95→304)。

        判据 = marker 锚 **或文字墨迹**逃出装箱 BAND(±5 容差):装箱"页内不
        重叠"的保证对 marker 同样成立,marker 出带即兜底桩把装箱几何打破;
        锚在带内而横排文字伸出(run-885b01f68b1f:ST2_IN2 锚 70、文字伸到
        -20)同样打破。判据 ②(2026-08-31):marker 墨迹矩形(_mark_rect,含
        文字翼)压任何器件本体——P3 reverse_VIN 落 REVERSER1 正中、P5 U3 脚4
        GND 旗压 U3 本体、J2 的 A6 网口文字压 R8 皆此形;rail 网不进紧凑化,
        GND 旗落哪算哪此前没有任何质量关。
        归属配对:同网、最近且 ≤320 的脚(2026-08-31 起**不再要求共轴**:
        拉移/clamp 拖着桩+标记斜甩后脚-marker 已不共轴,共轴判据会把出纸
        标记永久跳过——P4 LED6 的 netport 翼甩到 y=-116 即此;重落本就按脚
        重导正交桩,配对只要找对脚);配不上就跳过
        不动(宁留长桩不留断网)。重落走 _connect_stub 确定性落桩(方向=离带边
        远侧,文字翼展入带、墨迹避本体);失败退 planner autoconnect 保连通。
        全部入审计。
        """
        from edaloop.generate import packer
        from edaloop.generate.adapter import AdapterError

        bx1, by1, bx2, by2 = packer.BAND
        try:
            comps, _deg = self._list_components(page)
        except Exception as e:  # noqa: BLE001
            self.audit.event("freeze-pack-reseat", round_no=round_no, page=page,
                             error=f"list:{str(e)[:80]}")
            return
        parts = [c for c in comps if c.get("componentType") == "part" and c.get("designator")]

        def _body(c: dict) -> tuple[float, float, float, float] | None:
            b = c.get("bbox")
            if isinstance(b, dict) and "minX" in b:
                return (float(b["minX"]), float(b["minY"]), float(b["maxX"]), float(b["maxY"]))
            xs = [float(p["x"]) for p in (c.get("pins") or []) if p.get("x") is not None]
            ys = [float(p["y"]) for p in (c.get("pins") or []) if p.get("x") is not None]
            if not xs:
                return None
            return (min(xs) - 10, min(ys) - 10, max(xs) + 10, max(ys) + 10)

        body_rects = [r for r in (_body(c) for c in parts) if r]
        # 全页标记墨迹矩形(触发③:标记互压——P5 J2.B1A12 的 GND 旗与 C13_N7
        # netport 文字重叠即此形;同网重复标记(REVERSER1 双 reverse_VIN)互压
        # 也算——一脚一标记是常态,压在一起不是)
        mark_rects_all = [
            (self._mark_rect(f), str(f.get("net") or f.get("name") or ""), f)
            for f in comps
            if f.get("componentType") in ("netport", "netflag", "netlabel")
            and f.get("x") is not None
        ]

        def _pair(n: str, mx: float, my: float):
            """归属:同网 + 最近的脚(≤320);不要求共轴(斜甩标记也要救)。"""
            owner, od = None, 1e18
            for c in parts:
                for p in c.get("pins") or []:
                    if str(p.get("net") or "") != n or p.get("x") is None:
                        continue
                    px, py = float(p["x"]), float(p["y"])
                    dd = abs(px - mx) + abs(py - my)
                    if dd < od:
                        owner, od = (c["designator"], str(p.get("pinNumber")), px, py), dd
            return (owner, od) if owner is not None and od <= 320 else (None, 0.0)

        todo: list[tuple] = []  # (mark, owner, net, mx, my)
        skipped: list[str] = []
        for f in comps:
            if f.get("componentType") not in ("netport", "netflag", "netlabel") \
                    or f.get("x") is None:
                continue
            net = str(f.get("net") or f.get("name") or "")
            mx, my = float(f["x"]), float(f["y"])
            anchor_out = (mx < bx1 - 5 or mx > bx2 + 5
                          or my < by1 - 5 or my > by2 + 5)
            on_body = False
            on_mark = False
            if not anchor_out:
                # 判据 ②:标记墨迹(符号+文字翼)压器件本体(自件或他件)。
                # 判据 ③:标记墨迹压他件标记墨迹。±2 容差:贴边擦过不算压。
                mrect = self._mark_rect(f)
                on_body = any(
                    mrect[0] < rx2 - 2 and mrect[2] > rx1 + 2
                    and mrect[1] < ry2 - 2 and mrect[3] > ry1 + 2
                    for (rx1, ry1, rx2, ry2) in body_rects
                )
                on_mark = any(
                    mrect[0] < r2[2] - 2 and mrect[2] > r2[0] + 2
                    and mrect[1] < r2[3] - 2 and mrect[3] > r2[1] + 2
                    for (r2, _n2, g) in mark_rects_all
                    if g is not f
                )
            if not anchor_out and not on_body and not on_mark:
                # ST2_IN2 锚 70 在带内,横排文字 80 宽伸到 -20,锚判据漏掉)。
                # 桩方向 = 配对脚→锚;配不上脚方向未知,只信锚点。
                owner, _od = _pair(net, mx, my)
                if owner is None:
                    continue
                k0 = self._mark_kind(str(f.get("componentType") or "netport"), net)
                span = self._mark_span(net, k0)
                vx, vy = mx - owner[2], my - owner[3]
                L = abs(vx) + abs(vy)
                if L < 1:
                    continue
                ix, iy = mx + vx / L * span, my + vy / L * span
                if bx1 - 5 <= ix <= bx2 + 5 and by1 - 5 <= iy <= by2 + 5:
                    continue
            else:
                owner, _od = _pair(net, mx, my)
                if owner is None:
                    skipped.append(f"{net}@{mx:.0f},{my:.0f}:no-pin")
                    continue
            todo.append((f, owner, net, mx, my))
        if not todo and not skipped:
            return
        if not self._open_page_for_edit(page, "freeze-pack-reseat", round_no):
            return
        reseated: list[dict] = []
        failed: list[str] = []
        # 电气端点避让集(2026-09-01 并网护栏):全页脚端点 + 全页标记锚点;
        # 每落一枚新标记就追加其锚点(同轮后续候选 live 避让——run-
        # 30c3833705a4 P4:盲退 C7_N4 撞上已落 GND 同锚 (910,460) 即缺这层)
        avoid_pts: list[tuple[float, float]] = [
            (float(q["x"]), float(q["y"]))
            for c in parts for q in (c.get("pins") or []) if q.get("x") is not None
        ] + [(float(g["x"]), float(g["y"]))
             for (_r, _n, g) in mark_rects_all]
        for f, owner, net, mx, my in todo:
            desig, pnum, px, py = owner
            kind = self._mark_kind(str(f.get("componentType") or "netport"), net)
            owner_comp = next((c for c in parts if c["designator"] == desig), None)
            opins = [(float(q["x"]), float(q["y"]))
                     for q in (owner_comp.get("pins") or []) if q.get("x") is not None]
            own = _body(owner_comp) if owner_comp else None
            try:
                self.adapter.run(["sch", "disconnect", "--pin", f"{desig}:{pnum}"])
                # 避让集 = 全部本体框 + 他件标记墨迹:标记墨迹不得压任何本体
                # (含自件——P3 reverse_VIN 压的就是自件)与他件标记;自件框
                # 经 own_body 豁免桩线检查(脚在自件边上,桩穿自件几何强制)
                avoid = body_rects + [r2 for (r2, _n2, g) in mark_rects_all if g is not f]
                r = self._connect_stub(f"{desig}:{pnum}", kind, net, px, py, opins,
                                       body_rects=avoid, own_body=own,
                                       avoid_pts=avoid_pts)
                if r is not None:
                    nax = px + (r[1] if r[0] == "right" else -r[1] if r[0] == "left" else 0)
                    nay = py + (r[1] if r[0] == "up" else -r[1] if r[0] == "down" else 0)
                    avoid_pts.append((nax, nay))
                    reseated.append({"pin": f"{desig}:{pnum}", "net": net,
                                     "mark": f"{mx:.0f},{my:.0f}",
                                     "dir": r[0], "off": r[1]})
                    continue
                st = self._guarded_autoconnect(
                    page, round_no, f"{desig}:{pnum}", kind, net, px, py, opins,
                    own, avoid, avoid_pts, tag="reseat-blind-guard")
                if st == "error":
                    failed.append(f"{desig}:{pnum}:{net}")
                    # 上生产后(收口序重构)与 compact 恢复失败同性质:断网计数
                    self._wire_breaks.append(f"{page}:{net}:{desig}:{pnum}:reseat-escape")
                    continue
                failed.append(f"{desig}:{pnum}:{net}:fallback")
                reseated.append({"pin": f"{desig}:{pnum}", "net": net,
                                 "mark": f"{mx:.0f},{my:.0f}", "dir": "auto", "off": 0})
            except AdapterError as e:
                failed.append(f"{desig}:{pnum}:{str(e)[:40]}")
                self._wire_breaks.append(f"{page}:{net}:{desig}:{pnum}:reseat-escape-ae")
        self.audit.event("freeze-pack-reseat", round_no=round_no, page=page,
                         reseated=reseated, skipped=skipped, failed=failed)
        # 盲退/异轮标记的并网护栏:同锚异网/锚压他件脚端点 → 拆脚确定性重落
        if reseated or failed:
            self._fix_marker_coincidences(page, round_no)

    def _fix_marker_coincidences(self, page: str, round_no: int) -> None:
        """标记端点并网护栏(2026-09-01,run-30c3833705a4 P4 真机定性)。

        planner 盲退(autoconnect)落标记无任何几何检查,可与已落标记**同锚
        异网**或把锚钉在他件脚端点上——端点重合即并网;电源网 isGlobal,一点
        并轨全局短接(P4 GND 旗与盲退 C7_N4 netport 同锚 (910,460) → GND↔5V
        并轨四页齐灭),且并网后网对象粘死,disconnect+重落也回不来(修复
        通道实证:JSWD1:4 重落 GND 桩仍入 5V 网)。本护栏在标记落定后重列
        一次,发现端点重合即拆配对脚、带电气端点避让确定性重落;修不掉的
        入审计交目检(freeze 档不断链,后续 reseat 轮还会再扫)。"""
        from edaloop.generate.adapter import AdapterError

        try:
            comps, _deg = self._list_components(page)
        except Exception as e:  # noqa: BLE001
            self.audit.event("mark-merge-guard", round_no=round_no, page=page,
                             error=f"list:{str(e)[:60]}")
            return
        parts = [c for c in comps if c.get("componentType") == "part" and c.get("designator")]
        marks = [f for f in comps
                 if f.get("componentType") in ("netport", "netflag", "netlabel")
                 and f.get("x") is not None]
        if not marks:
            return
        pins = [(float(p["x"]), float(p["y"]), c["designator"], str(p.get("pinNumber")),
                 str(p.get("net") or ""))
                for c in parts for p in (c.get("pins") or []) if p.get("x") is not None]
        anchors = [(float(f["x"]), float(f["y"]), str(f.get("net") or f.get("name") or ""))
                   for f in marks]
        # 触点:①同锚异网(两标记端点重合)②标记锚压异网脚端点(同网=自件
        # 配对脚,零距桩是合法形态不触发)
        bad: list[int] = []
        for i, (ax, ay, an) in enumerate(anchors):
            if any(an != bn and abs(ax - bx) <= 4.0 and abs(ay - by) <= 4.0
                   for j, (bx, by, bn) in enumerate(anchors) if j != i):
                bad.append(i)
                continue
            if any(pn != an and abs(qx - ax) <= 4.0 and abs(qy - ay) <= 4.0
                   for (qx, qy, _d, _p, pn) in pins):
                bad.append(i)
        if not bad:
            return
        if not self._open_page_for_edit(page, "mark-merge-guard", round_no):
            self.audit.event("mark-merge-guard", round_no=round_no, page=page,
                             error="open-failed", coincident=len(bad))
            return
        avoid_pts = [(qx, qy) for (qx, qy, _d, _p, _n) in pins] \
            + [(ax, ay) for (ax, ay, _n) in anchors]
        body_rects = []
        for c in parts:
            b = c.get("bbox")
            if isinstance(b, dict) and "minX" in b:
                body_rects.append((float(b["minX"]), float(b["minY"]),
                                   float(b["maxX"]), float(b["maxY"])))
        mark_rects = [self._mark_rect(f) for f in marks]
        fixed: list[str] = []
        failed: list[str] = []
        for i in bad:
            ax, ay, an = anchors[i]
            # 配对脚:先同网(重落场景网名可信)再几何最近兜底(并网后网名
            # 已被吞,只能几何配)——纯几何会把邻近异网脚错拆成重落标记网
            if not pins:
                failed.append(f"@{ax:.0f},{ay:.0f}:{an}:no-pin")
                continue
            pool = [(abs(qx - ax) + abs(qy - ay), qx, qy, d, p)
                    for (qx, qy, d, p, pn) in pins if pn == an] \
                or [(abs(qx - ax) + abs(qy - ay), qx, qy, d, p)
                    for (qx, qy, d, p, _n) in pins]
            _dd, px, py, desig, pnum = min(pool)
            owner_comp = next((c for c in parts if c["designator"] == desig), None)
            opins = [(float(q["x"]), float(q["y"]))
                     for q in ((owner_comp.get("pins") or []) if owner_comp else [])
                     if q.get("x") is not None]
            ob = owner_comp.get("bbox") if owner_comp else None
            own = (float(ob["minX"]), float(ob["minY"]),
                   float(ob["maxX"]), float(ob["maxY"])) \
                if isinstance(ob, dict) and "minX" in ob else None
            kind = self._mark_kind(str(marks[i].get("componentType") or "netport"), an)
            try:
                self.adapter.run(["sch", "disconnect", "--pin", f"{desig}:{pnum}"])
                r = self._connect_stub(f"{desig}:{pnum}", kind, an, px, py, opins,
                                       body_rects=(body_rects
                                                   + [r2 for j, r2 in enumerate(mark_rects)
                                                      if j != i]) or None,
                                       own_body=own,
                                       avoid_pts=avoid_pts)
                if r is not None:
                    nax = px + (r[1] if r[0] == "right" else -r[1] if r[0] == "left" else 0)
                    nay = py + (r[1] if r[0] == "up" else -r[1] if r[0] == "down" else 0)
                    avoid_pts.append((nax, nay))
                    fixed.append(f"{desig}:{pnum}:{an}@{ax:.0f},{ay:.0f}"
                                 f"->{r[0]}/{r[1]}")
                    continue
                st = self._guarded_autoconnect(
                    page, round_no, f"{desig}:{pnum}", kind, an, px, py, opins,
                    own, body_rects or None, avoid_pts, tag="merge-blind-guard")
                if st != "error":
                    fixed.append(f"{desig}:{pnum}:{an}@{ax:.0f},{ay:.0f}->fallback")
                else:
                    failed.append(f"{desig}:{pnum}:{an}")
                    self._wire_breaks.append(f"{page}:{an}:{desig}:{pnum}:merge-guard")
            except AdapterError as e:
                failed.append(f"{desig}:{pnum}:{str(e)[:40]}")
        self.audit.event("mark-merge-guard", round_no=round_no, page=page,
                         coincident=len(bad), fixed=fixed, failed=failed)

    def _fix_wrong_side_marks(self, page: str, round_no: int) -> None:
        """标记同侧扫尾(2026-09-01,run-cbdfa6d997bf 终态实测 199 检 10 违例)。

        用户规范「连线不得穿越器件本体——引脚在哪侧,网标记就该在哪侧」:
        外侧优先方向序(_connect_stub 改造)修好了确定性重落那部分,但 reseat
        失败退 planner 盲落(autoconnect)的标记无几何关(慢性病),终态仍有
        ~5% 锚在引脚**对侧**——桩线横穿本体。本扫尾在标记全部落定后重列一次:
        同网近距标记锚(≤260,且本脚是该标记最近同网脚=独占)落在本件中心
        坐标系的引脚对侧 → disconnect+带端点避让确定性重落;重落失败退
        autoconnect 原样接回保电气。共享标记(一旗侍二脚/块接口 netport)
        不动——重落位属多脚折中,动了只会拆东墙。审计 mark-side-guard。"""
        from edaloop.generate.adapter import AdapterError

        try:
            comps, _deg = self._list_components(page)
        except Exception as e:  # noqa: BLE001
            self.audit.event("mark-side-guard", round_no=round_no, page=page,
                             error=f"list:{str(e)[:60]}")
            return
        parts = [c for c in comps if c.get("componentType") == "part" and c.get("designator")]
        marks = [f for f in comps
                 if f.get("componentType") in ("netport", "netflag", "netlabel")
                 and f.get("x") is not None]
        if not marks:
            return
        opp = {"left": "right", "right": "left", "up": "down", "down": "up"}

        def _side(vx: float, vy: float) -> str:
            if abs(vx) >= abs(vy):
                return "right" if vx > 0 else "left"
            return "up" if vy > 0 else "down"

        # 候选:(desig, pin, net, px, py, own_body, 独占标记索引)
        pins_by_net: dict[str, list[tuple[float, float]]] = {}
        for c in parts:
            for p in c.get("pins") or []:
                if p.get("x") is not None and p.get("net"):
                    pins_by_net.setdefault(str(p["net"]), []).append(
                        (float(p["x"]), float(p["y"])))
        cands: list[tuple[str, str, str, float, float, tuple | None, int]] = []
        for c in parts:
            d = c["designator"]
            b = c.get("bbox")
            if isinstance(b, dict) and "minX" in b:
                cx = (float(b["minX"]) + float(b["maxX"])) / 2
                cy = (float(b["minY"]) + float(b["maxY"])) / 2
                own = (float(b["minX"]), float(b["minY"]),
                       float(b["maxX"]), float(b["maxY"]))
            else:
                xs = [float(q["x"]) for q in (c.get("pins") or []) if q.get("x") is not None]
                ys = [float(q["y"]) for q in (c.get("pins") or []) if q.get("x") is not None]
                if not xs:
                    continue
                own = (min(xs) - 10, min(ys) - 10, max(xs) + 10, max(ys) + 10)
                cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
            for p in c.get("pins") or []:
                if p.get("x") is None or not p.get("net"):
                    continue
                px, py, pn = float(p["x"]), float(p["y"]), str(p["net"])
                pool = [m for m in marks
                        if str(m.get("net") or m.get("name") or "") == pn]
                if not pool:
                    continue
                mi = min(range(len(marks)),
                         key=lambda i2: abs(float(marks[i2]["x"]) - px)
                         + abs(float(marks[i2]["y"]) - py)
                         if str(marks[i2].get("net") or marks[i2].get("name") or "") == pn
                         else 1e9)
                mx, my = float(marks[mi]["x"]), float(marks[mi]["y"])
                if abs(mx - px) + abs(my - py) > 260:
                    continue  # 远端 netport(块接口),不是本脚标记
                # 独占:本脚必须是该标记最近的同网脚(共享标记不动)
                same_net = pins_by_net.get(pn) or []
                own_dist = abs(mx - px) + abs(my - py)
                if any((qx, qy) != (px, py)
                       and abs(qx - mx) + abs(qy - my) < own_dist - 1e-9
                       for qx, qy in same_net):
                    continue
                if _side(mx - cx, my - cy) != opp[_side(px - cx, py - cy)]:
                    continue  # 同侧/垂直侧都合法(垂直=顺边出线)
                cands.append((d, str(p.get("pinNumber")), pn, px, py, own, mi))
        if not cands:
            return
        if not self._open_page_for_edit(page, "mark-side-guard", round_no):
            self.audit.event("mark-side-guard", round_no=round_no, page=page,
                             error="open-failed", wrongside=len(cands))
            return
        avoid_pts = [(qx, qy) for lst in pins_by_net.values() for qx, qy in lst] \
            + [(float(f["x"]), float(f["y"])) for f in marks]
        body_rects = [r for r in (
            (float(c["bbox"]["minX"]), float(c["bbox"]["minY"]),
             float(c["bbox"]["maxX"]), float(c["bbox"]["maxY"]))
            if isinstance(c.get("bbox"), dict) and "minX" in c.get("bbox", {})
            else None for c in parts) if r]
        mark_rects = [self._mark_rect(f) for f in marks]
        fixed: list[str] = []
        failed: list[str] = []
        for d, pnum, pn, px, py, own, mi in cands:
            kind = self._mark_kind(str(marks[mi].get("componentType") or "netport"), pn)
            opins = [(float(q["x"]), float(q["y"]))
                     for c in parts if c["designator"] == d
                     for q in (c.get("pins") or []) if q.get("x") is not None]
            try:
                self.adapter.run(["sch", "disconnect", "--pin", f"{d}:{pnum}"])
                r = self._connect_stub(f"{d}:{pnum}", kind, pn, px, py, opins,
                                       body_rects=(body_rects
                                                   + [r2 for j, r2 in enumerate(mark_rects)
                                                      if j != mi]) or None,
                                       own_body=own,
                                       avoid_pts=avoid_pts)
                if r is not None:
                    nax = px + (r[1] if r[0] == "right" else -r[1] if r[0] == "left" else 0)
                    nay = py + (r[1] if r[0] == "up" else -r[1] if r[0] == "down" else 0)
                    avoid_pts.append((nax, nay))
                    fixed.append(f"{d}:{pnum}:{pn}->{r[0]}/{r[1]}")
                    continue
                rc2, _o2, _e2 = self.adapter.run(
                    ["sch", "autoconnect", "--pin", f"{d}:{pnum}",
                     "--kind", kind, "--net", pn])
                if rc2 == 0:
                    failed.append(f"{d}:{pnum}:{pn}:fallback-kept")
                else:
                    failed.append(f"{d}:{pnum}:{pn}:lost")
                    self._wire_breaks.append(f"{page}:{pn}:{d}:{pnum}:side-guard")
            except AdapterError as e:
                failed.append(f"{d}:{pnum}:{str(e)[:40]}")
        self.audit.event("mark-side-guard", round_no=round_no, page=page,
                         wrongside=len(cands), fixed=fixed, failed=failed)

    def _overlap_reprobe(self, page: str, round_no: int,
                         members: dict[str, list[str]],
                         oversize: bool = False) -> None:
        """紧凑化后重叠/出纸复探+分离(2026-08-31)。

        为什么需要:收口序 rotate→reseat→closeout→compact,最后一程
        compact 的 _pull_long_pairs 还在移动器件——closeout 探针清零的页会被
        compact 的拉近重新弄叠(P4 LED3↔LED6 本体全叠 21×26+pin2 同点隐短、
        P6 R19↔LED9 38×11/LED10 顶沿 876>813 出纸,全是 compact 之后才成形,
        closeout 探针永远看不见)。
        口径:clusters findings 是**体积框**(含文字翼)——翼碰翼/标记出纸
        不在此动(交给 reseat 压体/斜甩重落),**本体相交/本体出纸才分离**:
        本体叠是隐短的真身,标记叠是观感。逐次:探 → 挑一条 → 找空位(4 正向
        先于 4 斜向 × 40..320 阶梯,避全部本体余量 15、不出 [12,12,1158,813],
        位移最小优先——不是盲推 ±40,那是把件推上第三件的旧病)→ group-move
        刚移(挂线跟随但不保网:移前快照脚网,移后丢网按新位重落,同
        pull-netloss 口径;被拒退 modify 改位回退,restub 内置)→ 重探。
        一次一动,上限 6 防呆;无空位如实停,remaining 交 review。
        oversize 页跳过 out-of-sheet(块高出 A4 带是装箱定案,钳它=错,同
        closeout 豁免)。自画线 bbox 记账同步平移(框口径要罩住拖动的线)。
        """
        from edaloop.generate.adapter import AdapterError

        des_block = {d: inst for inst, ds in (members or {}).items() for d in ds}
        moves: list[dict] = []
        for _step in range(6):
            rep = self._clusters_report(page)
            errs = [f for f in (rep.get("findings") or []) if f.get("level") == "ERROR"]
            if not errs:
                break
            try:
                comps, _deg = self._list_components(page)
            except Exception as e:  # noqa: BLE001
                self.audit.event("overlap-reprobe", round_no=round_no, page=page,
                                 error=f"list:{str(e)[:80]}", moves=moves)
                return
            parts = [c for c in comps if c.get("componentType") == "part"
                     and c.get("designator")]

            def _body(c: dict):
                b = c.get("bbox")
                if isinstance(b, dict) and "minX" in b:
                    return (float(b["minX"]), float(b["minY"]),
                            float(b["maxX"]), float(b["maxY"]))
                xs = [float(p["x"]) for p in (c.get("pins") or []) if p.get("x") is not None]
                ys = [float(p["y"]) for p in (c.get("pins") or []) if p.get("x") is not None]
                return (min(xs) - 10, min(ys) - 10, max(xs) + 10, max(ys) + 10) if xs else None

            boxes = {c["designator"]: _body(c) for c in parts}
            boxes = {d: b for d, b in boxes.items() if b}

            def _body_hit(r1, r2, m: float = 2.0) -> bool:
                return (r1[0] < r2[2] - m and r1[2] > r2[0] + m
                        and r1[1] < r2[3] - m and r1[3] > r2[1] + m)

            mover: tuple[str, float, float, str] | None = None
            for f in errs:
                t = f.get("type")
                if t == "out-of-sheet":
                    if oversize:
                        continue  # 块高出带=装箱定案,钳它=错(closeout 同豁免)
                    d = str(f.get("a") or "")
                    b = boxes.get(d)
                    if b and (b[0] < 12 or b[1] < 12 or b[2] > 1158 or b[3] > 813):
                        mover = (d, 0.0, 0.0, t)  # 位移由 _slot_for 定
                        break
                elif t == "overlap":
                    a, b_ = str(f.get("a") or ""), str(f.get("b") or "")
                    if a in boxes and b_ in boxes and _body_hit(boxes[a], boxes[b_]):
                        # 动小件(脚少者,平局按体积小者):扰动最小,同拉近锚/伴分工
                        area = lambda d: (boxes[d][2] - boxes[d][0]) * (boxes[d][3] - boxes[d][1])  # noqa: E731
                        npins = {c["designator"]: len([p for p in c.get("pins") or []
                                                       if p.get("x") is not None])
                                 for c in parts}
                        pick = min((a, b_), key=lambda d: (npins.get(d, 0), area(d), d))
                        mover = (pick, 0.0, 0.0, t)
                        break
            if mover is None:
                break  # 剩余 ERROR 都是翼级(标记/文字),交给 reseat;不再空转
            d = mover[0]
            box = boxes[d]

            def _slot_for() -> tuple[float, float] | None:
                best: tuple[float, float, float] | None = None
                for dvx, dvy in ((1, 0), (-1, 0), (0, 1), (0, -1),
                                 (1, 1), (1, -1), (-1, 1), (-1, -1)):
                    for off in (40, 80, 120, 160, 200, 240, 280, 320):
                        dx, dy = dvx * off, dvy * off
                        nb = (box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy)
                        if nb[0] < 12 or nb[1] < 12 or nb[2] > 1158 or nb[3] > 813:
                            continue
                        if any(_body_hit(nb, ob, 15.0) for od, ob in boxes.items()
                               if od != d):
                            continue
                        s = abs(dx) + abs(dy)
                        if best is None or s < best[0]:
                            best = (s, dx, dy)
                return (best[1], best[2]) if best else None

            slot = _slot_for()
            if slot is None:
                self.audit.event("overlap-reprobe", round_no=round_no, page=page,
                                 moves=moves, stuck=f"{mover[3]}:{d}:no-slot")
                return
            dx, dy = slot
            mc = next((c for c in parts if c["designator"] == d), {})
            gid = self._isolate_designator(page, round_no, d)
            if not gid:
                self.audit.event("overlap-reprobe", round_no=round_no, page=page,
                                 moves=moves, stuck=f"{d}:no-group")
                return
            snap = {str(p.get("pinNumber") or ""): str(p.get("net") or "")
                    for p in (mc.get("pins") or [])}
            try:
                rc, _out, err = self.adapter.run(
                    ["sch", "group-move", "--group", gid, "--dx", str(dx),
                     "--dy", str(dy), "--doc", page])
            except AdapterError:
                rc, err = 1, "adapter"
            via = "group-move" if rc == 0 else ""
            if rc != 0 and self._pull_modify_move(page, round_no, d, mc, dx, dy):
                via = "modify"  # 内置拆桩+改位+按新位 restub
            if not via:
                self.audit.event("overlap-reprobe", round_no=round_no, page=page,
                                 moves=moves, stuck=f"{d}:move-refused",
                                 error=(err or "")[-120:])
                return
            moves.append({"moved": d, "dx": dx, "dy": dy, "via": via,
                          "cause": mover[3]})
            # 自画线记账平移(块归属两端任一为本件都要挪——框要罩住拖动的线)
            inst = des_block.get(d)
            if inst:
                self._wire_boxes[page] = [
                    (ia, ib, (bb[0] + dx, bb[1] + dy, bb[2] + dx, bb[3] + dy))
                    if inst in (ia, ib) else (ia, ib, bb)
                    for ia, ib, bb in self._wire_boxes.get(page, [])
                ]
            # group-move 不保网:移后回读,丢网脚按新位重落(modify 路径 restub
            # 已内置,仍统一比对——多一道保险不花钱)
            try:
                comps_v, _ = self._list_components(page)
                cur = next((c for c in comps_v
                            if c.get("designator") == d
                            and c.get("componentType") == "part"), None)
            except Exception:  # noqa: BLE001
                cur = None
            if cur is not None:
                cur_nets = {str(p.get("pinNumber") or ""): str(p.get("net") or "")
                            for p in cur.get("pins") or []}
                pin_xy = {str(p.get("pinNumber")): (float(p["x"]), float(p["y"]))
                          for p in cur.get("pins") or [] if p.get("x") is not None}
                stubs = [(f"{d}:{pn}", *(pin_xy.get(pn, (0.0, 0.0))), n)
                         for pn, n in snap.items()
                         if n and not cur_nets.get(pn, "")]
                if stubs:
                    try:
                        self._restub_net_pins(page, round_no, d, cur, stubs)
                    except Exception as e:  # noqa: BLE001
                        self.audit.event("overlap-reprobe", round_no=round_no, page=page,
                                         moves=moves, error=f"restub:{str(e)[:80]}")
        if moves:
            rep_end = self._clusters_report(page)
            remaining = len([f for f in (rep_end.get("findings") or [])
                             if f.get("level") == "ERROR"])
            self.audit.event("overlap-reprobe", round_no=round_no, page=page,
                             moves=moves, remaining=remaining)

    def _draw_module_frames(self, round_no: int, actions, placed_by_page) -> None:
        """生产模式画模块框(v0.6.11 审计 P1,默认开,EDALOOP_LAYOUT_FRAMES=0 关)。
        口径=volume(clusters box:器件+自有 marker 文字+桩线)∪ 本轮自画直连线
        bbox——用户口径"蓝框必须框住连线网格与最远处文字"。此前只在 freeze
        目检画框,生产交付页没有框:zones 默认关,成品图既无模块边界也无尺寸
        参考(用户目检主诉求)。复用 _freeze_trial_frames 的画框原语;失败只
        审计不判负(注释层,非电气对象,不挡 gate)。"""
        from edaloop.generate.adapter import AdapterError

        for pg in self._plan_pages(actions):
            insts = placed_by_page.get(pg) or {}
            if not insts:
                continue
            try:
                _rc, out, _err = self.adapter.run(["sch", "clusters", "--json", "--doc", pg])
                rep = json.loads(out) if (out or "").strip() else {}
            except (AdapterError, ValueError):
                rep = {}
            comp_boxes = {c.get("designator"): (c.get("box") or c.get("body"))
                          for c in rep.get("clusters") or []
                          if c.get("designator") and (c.get("box") or c.get("body"))}
            if not comp_boxes:
                self.audit.event("module-frames", round_no=round_no, page=pg,
                                 drawn=False, error="clusters 无可框项")
                continue
            wb: dict[str, list[float]] = {}
            for _ia, _ib, _bb in self._wire_boxes.get(pg, []):
                for i2 in {_ia, _ib}:
                    if i2 not in insts:
                        continue
                    w = wb.setdefault(i2, [1e9, 1e9, -1e9, -1e9])
                    w[0] = min(w[0], _bb[0])
                    w[1] = min(w[1], _bb[1])
                    w[2] = max(w[2], _bb[2])
                    w[3] = max(w[3], _bb[3])
            items: list[tuple] = []
            for inst, desigs in insts.items():
                mb = [comp_boxes[d] for d in desigs if d in comp_boxes]
                if not mb:
                    continue
                fx1 = min(b["minX"] for b in mb)
                fy1 = min(b["minY"] for b in mb)
                fx2 = max(b["maxX"] for b in mb)
                fy2 = max(b["maxY"] for b in mb)
                w = wb.get(inst)
                if w and w[0] < 1e9:
                    fx1, fy1 = min(fx1, w[0]), min(fy1, w[1])
                    fx2, fy2 = max(fx2, w[2]), max(fy2, w[3])
                items.append((inst, fx1, fy1, fx2, fy2, False))
            if items:
                self._freeze_trial_frames(round_no, items, page=pg)

    def _freeze_trial_frames(self, round_no: int, items: list[tuple],
                             page: str = "P1", band_rect: bool = False) -> None:
        """在指定页把每块的实测/估算包围盒画成虚线矩形 + 左上坐标/长宽标注。

        原语与上游 zone-draw 同源:sch_PrimitiveRectangle.create(x, 上沿y, w, h,
        0, 0, color, null, 1, 1) 是虚线框;文字锚左上(x+4, 上沿-fontSize)。
        实测框蓝 / 估算框橙;band_rect=True 附带画装箱带 BAND 参考框(绿)。
        失败自清(删已建图元再报错),判定只信回读语义由 debug exec 的 JS
        返回承载。目检后手工恢复:sch clear --doc <page>。
        """
        try:
            _rc, out, _err = self.adapter.run(["sch", "pages"])
            pages = (json.loads(out) or {}).get("result", {}).get("pages") or []
            puuid = next((str(p.get("uuid")) for p in pages if p.get("name") == page), "")
            if puuid:
                self.adapter.run(["sch", "open", "--page", puuid])
            else:
                self.audit.event("trial-freeze-open", round_no=round_no,
                                 error=f"{page} uuid 未解析,画在当前活动页")
        except Exception as e:  # noqa: BLE001
            self.audit.event("trial-freeze-open", round_no=round_no, error=str(e)[:150])
        parts: list[str] = []
        if band_rect:
            from edaloop.generate import packer  # 局部 import:与 _repack_actions 同约定

            bx1, by1, bx2, by2 = packer.BAND
            bw, bh = bx2 - bx1, by2 - by1
            parts.append(
                "{ const rc = await eda.sch_PrimitiveRectangle.create(%g, %g, %g, %g, 0, 0, %s, null, 1, 1);"
                " if (!rc) throw new Error('rect BAND');"
                " rects.push(rc.getState_PrimitiveId());"
                " const tt = await eda.sch_PrimitiveText.create(%g, %g, %s, 0, %s, null, 14);"
                " if (!tt) throw new Error('text BAND');"
                " texts.push(tt.getState_PrimitiveId()); }"
                % (bx1, by2, bw, bh, json.dumps("#2CA02C"), bx1 + 4, by2 - 14,
                   json.dumps(f"BAND ({bx1:.0f},{by2:.0f}) {bw:.0f}x{bh:.0f}"), json.dumps("#2CA02C"))
            )
        for inst, x1, y1, x2, y2, is_est in items:
            w, h = x2 - x1, y2 - y1
            if w <= 0 or h <= 0:
                continue
            color = "#FF7F0E" if is_est else "#1E90FF"
            label = f"{'est ' if is_est else ''}{inst} ({x1:.0f},{y2:.0f}) {w:.0f}x{h:.0f}"
            parts.append(
                "{ const rc = await eda.sch_PrimitiveRectangle.create(%g, %g, %g, %g, 0, 0, %s, null, 1, 1);"
                " if (!rc) throw new Error('rect %s');"
                " rects.push(rc.getState_PrimitiveId());"
                " const tt = await eda.sch_PrimitiveText.create(%g, %g, %s, 0, %s, null, 14);"
                " if (!tt) throw new Error('text %s');"
                " texts.push(tt.getState_PrimitiveId()); }"
                % (x1, y2, w, h, json.dumps(color), inst, x1 + 4, y2 - 14,
                   json.dumps(label), json.dumps(color), inst)
            )
        code = (
            "const rects=[], texts=[];\ntry {\n" + "\n".join(parts)
            + "\n  return {ok:true, rects, texts};\n"
            "} catch (err) {\n"
            "  for (const id of rects) { try { await eda.sch_PrimitiveObject.delete([id]); } catch (e) {} }\n"
            "  for (const id of texts) { try { await eda.sch_PrimitiveObject.delete([id]); } catch (e) {} }\n"
            "  return {ok:false, error: String(err), rolledBack:{rects:rects.length, texts:texts.length}};\n}"
        )
        rc, out, err = self.adapter.run(["debug", "exec", "--timeout", "60", "--code", code])
        drawn = False
        try:
            payload = json.loads(out) or {}
            # debug exec 的 JS 返回值嵌在 result.value 下(真机 2026-08-25 实测形状)
            drawn = bool(payload.get("ok") and (payload.get("result") or {}).get("value", {}).get("ok"))
        except ValueError:
            pass
        self.audit.event(
            "trial-freeze", round_no=round_no, page=page, blocks=len(items),
            drawn=drawn, rc=rc, err=(err or "")[:150],
        )

    def _clear_page_verified(self, page: str, round_no: int) -> bool:
        """clear --doc 后机械复核:result.remaining(自报)+ 回读数器件(实证)双证据;
        任一不净 → 重清一次;两趟仍不清 → False(审计留痕)。"""
        for attempt in (1, 2):
            rc, out, _ = self.adapter.run(["sch", "clear", "--doc", page])
            remaining: int | None = None
            warnings: list[str] = []
            try:
                result = (json.loads(out) or {}).get("result", {}) or {}
                remaining = result.get("remaining")
                warnings = [str(w)[:200] for w in (result.get("warnings") or [])][:3]
            except ValueError:
                pass  # 非 JSON 输出:remaining 维持未知,判定交给回读
            survivors = self._page_component_count(page)
            ok = rc == 0 and survivors == 0 and remaining in (None, 0)
            self.audit.event(
                "page-clear-doc",
                round_no=round_no,
                page=page,
                rc=rc,
                remaining=remaining,
                survivors=survivors,
                warnings=warnings,
                attempt=attempt,
                ok=ok,
            )
            if ok:
                return True
        return False

    @staticmethod
    def _first_uuid(resp: dict) -> tuple[str, str]:
        res = resp.get("result", {}) or {}
        comps = res.get("components") or res.get("results") or []
        for r in comps:
            lib = r.get("libraryUuid") or r.get("lib") or ""
            uuid = r.get("uuid") or r.get("deviceUuid") or ""
            if lib and uuid:
                return lib, uuid
        return "", ""

    def _warmup(self, attempts: int = 3, delay: float = 5.0) -> None:
        """廉价读命令预热连接器 WS;全部失败则重解析窗口再试一轮。"""
        for phase in ("first", "refresh"):
            for _ in range(attempts if phase == "first" else 2):
                try:
                    rc, out, _ = self.adapter.run(["sch", "pages"])
                    if rc == 0:
                        return
                except Exception:
                    pass
                time.sleep(delay)
            if phase == "first":
                self.adapter.refresh_window()
                self.audit.event("window-refresh", round_no=None)

    def _run_manifest_once(self, args) -> dict:
        """变更型命令(block-apply)只许执行一次,manifest 取该次 stdout。

        禁止走 _run_json_retry 重发:同参 block-apply 重放会在已落图的页上
        再放一份同几何部件(平台只给新位号 D4/J2/R3…),apply 内置 verify
        逐件报 overlap+pin coincidence → 整单判死回滚,第一遍成品被误记
        failed-rolled-back;若重放的推让恰好躲开同位,verify 漏抓、双份留
        页,拖到逐页 gate 才炸(run3b r1/r2 六连挂根因;2026-08-21 连接器
        审计对账 + 活体复现 D1↔D4…R2↔R4 六对孪生后钉死)。"""
        from edaloop.generate.adapter import AdapterError

        rc, out, err = self.adapter.run(args)
        try:
            return json.loads(out) if out.strip() else {}
        except ValueError as e:
            raise AdapterError(
                f"block-apply stdout 非 JSON(rc={rc},{e}):stderr 尾部={err[-500:]}"
            ) from e

    # 连接器 wedge 特征(上游定性 2026-08-24):EFFECT 通道假死时 CLI stderr 带
    # 这些标记——与逻辑失败区分,值得 refresh 窗口钉扎 + 长等降载后再试。
    _WEDGE_MARKERS = ("DEGRADED", "did not respond", "no connected window")
    # 落-量-清 的块间歇(秒):给上游 webview 保存/重绘风暴留排水口,单测置 0
    _MEASURE_PACE = 2.0

    def _connector_wedged(self, err: Exception | str) -> bool:
        return any(m in str(err) for m in self._WEDGE_MARKERS)

    def _run_json_retry(self, args, attempts: int = 2, delay: float = 8.0) -> dict:
        from edaloop.generate.adapter import AdapterError

        last: Exception | None = None
        for i in range(attempts):
            try:
                return self.adapter.run_json(args)
            except AdapterError as e:
                last = e
                self.audit.event("apply-error", attempt=i + 1, error=str(e)[:2000])
                if i + 1 < attempts:
                    if self._connector_wedged(e):
                        # wedge 不吃短重试:风暴还在降不下来,refresh 钉扎+长等
                        refresh = getattr(self.adapter, "refresh_window", None)
                        if refresh:
                            refresh()
                        time.sleep(30)
                    else:
                        time.sleep(delay)
        raise last
