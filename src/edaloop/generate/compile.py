from __future__ import annotations

from edaloop.generate.models import Action, BlockPlan
from edaloop.knowledge.models import BlockRecord

_GND_HINTS = ("GND", "AGND", "DGND", "PGND", "E", "VSS")
_PWR_HINTS = ("VCC", "VDD", "COM", "VBAT", "VIN", "VSYS")


def _sanitize_designator(instance: str) -> str:
    d = "".join(c for c in instance.upper() if c.isalnum())
    return d[:8] if d else "U1"


class CompileError(Exception):
    pass


def _pin_kind(pin_name: str) -> str:
    n = pin_name.upper()
    if any(h in n for h in _GND_HINTS):
        return "gnd"
    if any(h in n for h in _PWR_HINTS):
        return "power"
    return "netport"


def _fill_bindings(plan: BlockPlan, catalog: dict[str, BlockRecord]) -> BlockPlan:
    for b in plan.blocks:
        rec = catalog.get(b.block_id)
        if rec is None:
            raise CompileError(f"块 {b.block_id} 不在库中")
        if rec.upstream is not None:
            if b.upstream_id != rec.upstream.id:
                raise CompileError(
                    f"块 {b.block_id} 的 upstream_id {b.upstream_id} 与库中 {rec.upstream.id} 不一致"
                )
            ports = rec.upstream.ports
            unknown = [p for p in b.ports_binding if p not in ports]
            if unknown:
                raise CompileError(f"块 {b.block_id} 绑定了不存在的端口: {unknown}")
            for port, default_net in ports.items():
                b.ports_binding.setdefault(port, default_net)
        else:
            if not rec.lcsc:
                raise CompileError(f"块 {b.block_id} 无 upstream 且无 lcsc(不可落图)")
            if not b.pins_binding:
                raise CompileError(
                    f"块 {b.block_id} 是库外器件(place 通道),必须给出 pins_binding(pin号→网络)"
                )
            if rec.pinout:
                unknown = [p for p in b.pins_binding if p not in rec.pinout]
                if unknown:
                    raise CompileError(f"块 {b.block_id} 绑定了不存在的引脚号: {unknown}")
    return plan


_GRID_X0 = 400
_GRID_Y0 = 300

# ---- P4-1① 功能分区:category → 带(band)。带 = 上游全高区词汇 left/center/right,
# 三带平铺不重叠;zoneRect 按 live sheet bbox 解析,带内内容超出 A4 时声明仍成立(溢出由 zone-plan 报)。
# 信号流向约定:电源入口在左 → 主控居中 → 接口/外设在右。
_CATEGORY_BAND = {
    "power": 0, "usb": 0, "human-input": 0, "speaker-amp": 0, "audio": 0,
    "mcu": 1, "mcu-support": 1, "timing": 1, "driver": 1, "imu": 1, "sensing": 1, "passive": 1,
    "rf": 2, "comms": 2, "interface": 2, "storage": 2, "indicator": 2, "display": 2,
}
# 带 → 分区声明名。声明名同时是 note --zone 的挂靠名。
_BAND_CLAIMS = {0: "PWR", 1: "MCU", 2: "PERI"}
# 声明名 → (上游 zones 词汇, 中文说明):controller 组 zones set / zone note 用
CLAIM_ZONE = {"PWR": ("left", "电源"), "MCU": ("center", "主控"), "PERI": ("right", "外设接口")}
# planner 显式给的 zone(上游 9 格词汇)→ 归一到水平带
_ZONE_BAND = {
    "left": 0, "center": 1, "right": 2,
    "left-top": 0, "left-bottom": 0, "center-top": 1, "center-bottom": 1,
    "right-top": 2, "right-bottom": 2,
}
_SHEET_TOP_LIMIT = 8200  # 名义 A4 高;y-UP,超限换列(zone-plan sheetOverflow 会报,不静默)
_BAND_GAP = 300

# 上游块的保守占位估计(格距, 与块内器件数×spacing 成正比;v2 布局策略的输入)
# 数据源:721 次 block-apply 审计分析——spacing 400 失败率 70%,600 仅 3%(见 P1 八批变更记录)
_BLOCK_CELL = {
    "block.esp32s3_wroom1_module": 2800,
    "block.ch340c_usb_serial": 2800,
    "block.lgs4056_liion_charge_path": 2800,
    "block.vehicle_input_tps54360_5v": 2800,
    "block.usbc_ufp_power_or": 2400,
    "block.usbc_dual_orientation_data": 2400,
    "block.sp3485_rs485_halfduplex": 2600,
    "block.ams1117_ldo_3v3": 1600,
    "block.tactile_boot_reset": 1400,
    "block.led_indicator_gpio": 1200,
    "block.esp32_autodownload": 1600,
    "block.sy7088_boost_5v": 1800,
}
_CELL_DEFAULT = 2200
_CELL_PLACE = 900
_CELL_ROW_EXTRA = 400
_CALIB_SPACING = 600  # _BLOCK_CELL 的标定间距


def _cell_for(rec: BlockRecord, spacing: int = _CALIB_SPACING) -> tuple[int, int]:
    """占位估计随 spacing 线性缩放(P4-1③ 硬伤 B:间距放大时占位表不再低估)。"""
    s = max(int(spacing), 100) / _CALIB_SPACING
    if rec.upstream is not None:
        base = _BLOCK_CELL.get(rec.upstream.id, _CELL_DEFAULT)
        return int((base + _CELL_ROW_EXTRA) * s), int(base * s)
    place = int(_CELL_PLACE * s)
    return place, place


def band_of(rec: BlockRecord, zone_hint: str = "") -> int:
    """块归属带:planner 显式 zone 优先,否则按 category 默认,未知 category 落主控带。"""
    hint = (zone_hint or "").strip().lower()
    if hint in _ZONE_BAND:
        return _ZONE_BAND[hint]
    return _CATEGORY_BAND.get((rec.category or "").strip().lower(), 1)


class _BandFlow:
    """带内自底向上单列流式布局,列满换列(P4-1③ 硬伤 A:堆叠只看自身 dy,同列无跨块行共享)。

    y-UP:从 _GRID_Y0 向上堆;下一块起点 = 前一块起点 + 前一块自身 dy(逐块推进,
    不存在"换行用触发块 dy"的共享行问题);累计越过 top_limit 换新列,列进给统一槽宽。
    """

    def __init__(self, anchor_x: int, col_w: int, top_limit: int = _SHEET_TOP_LIMIT) -> None:
        self.anchor_x = anchor_x
        self.col_w = max(col_w, 100)
        self.top_limit = top_limit
        self.col = 0
        self.next_y = _GRID_Y0
        self.max_dx = 0
        self.placed_any = False

    def take(self, dx: int, dy: int) -> str:
        if self.placed_any and self.next_y + dy > self.top_limit:
            self.col += 1
            self.next_y = _GRID_Y0
        at = f"{self.anchor_x + self.col * self.col_w},{self.next_y}"
        self.next_y += dy
        self.max_dx = max(self.max_dx, dx)
        self.placed_any = True
        return at

    @property
    def width(self) -> int:
        """列数 × 带内最宽块(probe 估算带宽用;真实放置的列进给统一槽宽)。"""
        return (self.col + 1) * self.max_dx if self.placed_any else 0


def compile_actions(
    plan: BlockPlan,
    catalog: dict[str, BlockRecord],
    *,
    spacing_default: str = "600",
) -> list[Action]:
    plan = _fill_bindings(plan, catalog)
    actions: list[Action] = []
    spacing = int(spacing_default)
    # P4-1①:按带分组(带内保持 plan 顺序)。zoneRect 按 live bbox 三等分解析,
    # 故三带用统一槽宽(取各带内容宽度最大值)+等距锚点,让各带内容落在自己的 1/3 内。
    band_blocks: dict[int, list] = {0: [], 1: [], 2: []}
    for b in plan.blocks:
        band_blocks[band_of(catalog[b.block_id], b.zone)].append(b)
    band_widths: dict[int, int] = {}
    for band in (0, 1, 2):
        probe = _BandFlow(0, col_w=1)
        for b in band_blocks[band]:
            dx, dy = _cell_for(catalog[b.block_id], spacing)
            probe.take(dx, dy)
        band_widths[band] = probe.max_dx  # 带的单列自然宽;多列带溢出自身 1/3 仅 WARN
    slot_w = max(band_widths.values())
    for band in (0, 1, 2):
        flows = _BandFlow(_GRID_X0 + band * (slot_w + _BAND_GAP), col_w=slot_w)
        for b in band_blocks[band]:
            dx, dy = _cell_for(catalog[b.block_id], spacing)
            if not b.at:
                b.at = flows.take(dx, dy)
    for b in plan.blocks:
        rec = catalog[b.block_id]
        band = band_of(rec, b.zone)
        if rec.upstream is not None:
            args = [
                "sch",
                "block-apply",
                b.upstream_id,
                "--instance",
                b.instance,
                "--spacing",
                spacing_default,
                "--at",
                b.at,
            ]
            for port, net in b.ports_binding.items():
                args += ["--bind", f"{port}={net}"]
            args.append("--json")
            actions.append(
                Action(
                    kind="block-apply",
                    block_instance=b.instance,
                    upstream_id=b.upstream_id,
                    args=args,
                    desc=f"{rec.name} @ {b.at} -> {b.ports_binding}",
                    zone=_BAND_CLAIMS[band],
                )
            )
        else:
            designator = _sanitize_designator(b.instance)
            x, y = b.at.split(",")
            b.params["x"], b.params["y"] = x, y
            place = [
                "sch",
                "place",
                "--lib",
                b.params.get("lib_uuid", ""),
                "--uuid",
                b.params.get("device_uuid", ""),
                "--x",
                b.params.get("x", "400"),
                "--y",
                b.params.get("y", "300"),
                "--designator",
                designator,
            ]
            actions.append(
                Action(
                    kind="lib-search",
                    block_instance=b.instance,
                    lcsc=rec.lcsc or "",
                    mpn=(rec.parts[0].ref if rec.parts else ""),
                    args=["lib", "search", "--query", rec.lcsc or "", "--limit", "3"],
                    desc=f"查 {rec.lcsc} 的库 uuid(place 前置,C 号无映射时回退 MPN)",
                )
            )
            actions.append(
                Action(
                    kind="sch-place",
                    block_instance=b.instance,
                    args=place,
                    pinout=dict(rec.pinout) if rec.pinout else None,
                    desc=f"{rec.name}({rec.lcsc}) 直放",
                    zone=_BAND_CLAIMS[band],
                )
            )
            pinout = rec.pinout or {}
            for pin, net in b.pins_binding.items():
                pin_name = pinout.get(pin, pin)
                kind = _pin_kind(pin_name)
                actions.append(
                    Action(
                        kind="sch-autoconnect",
                        block_instance=b.instance,
                        args=[
                            "sch",
                            "autoconnect",
                            "--pin",
                            f"{designator}:{pin_name}",
                            "--kind",
                            kind,
                            "--net",
                            net,
                        ],
                        desc=f"{designator}:{pin}({pin_name}) -> {net}",
                    )
                )
    actions.append(
        Action(kind="sch-gate", block_instance="", upstream_id="", args=["sch", "gate", "--json"], desc="验证门禁")
    )
    return actions
