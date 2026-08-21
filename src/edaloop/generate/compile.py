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


# A4 横放实测(EasyEDA 单位 = 0.01 inch):1170 × 825,y-UP;图签占位右下 [468..1170, 0..198]。
# 锚点(100,300):墨迹左/下探 ~20,避图签(y≥198)且贴边距。
_GRID_X0 = 100
_GRID_Y0 = 300
_SHEET_TOP_LIMIT = 800  # 顶部余 25;esp32 类 489 高块 300..789 可整页放下
_SHEET_W = 1170  # A4 横放全宽(墨迹右缘校核用)
_STACK_GAP = 60  # 同页相邻块垂直间隙

# ---- P4-1① 功能分区:category → 带(band)。带 = 上游 zones 词汇 left/center/right 的认领组。
# P4-b2 起 A4 尺度下三带由「横向并排」改为「纵向堆叠次序」:spacing 250 实测块墨迹宽
# 821~921,单页宽只容一列;信号流向仍为 电源入口 → 主控 → 接口/外设(0→1→2),
# 页满开新页,controller 以 --doc 钉扎落图页(跨页同名网 port 电气等价)。
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

# 实测墨迹占位表:(dx, dy) = sch list --include-bbox 的器件 bbox 并集,spacing 250、
# --at 100,300、含默认 --bind(2026-08-21 真机标定,31/32 块;证据 .claude/measure-ink-full.json)。
# 取代旧 _BLOCK_CELL 估算表(超幅画布期产物,系统性高估 ~4×);dy 随 spacing 线性缩放。
# ch334f_usb4_hub 两轮 apply 失败未测得,走 _INK_DEFAULT 保守值。
_INK_CELL = {
    "block.usbc_ufp_power_or": (831, 318),
    "block.vehicle_input_tps54360_5v": (864, 1384),
    "block.lgs4056_liion_charge_path": (871, 846),
    "block.sy7088_boost_5v": (831, 589),
    "block.ams1117_ldo_3v3": (856, 41),
    "block.esp32s3_wroom1_module": (831, 489),
    "block.ch340c_usb_serial": (861, 356),
    "block.sp3485_rs485_halfduplex": (846, 594),
    "block.led_indicator_gpio": (136, 26),
    "block.tactile_boot_reset": (311, 10),
    "block.aw8737_classd_spk": (856, 596),
    "block.bmi270_imu_i2c": (856, 81),
    "block.bq24074_powerpath_charger": (861, 904),
    "block.cc1101_433m_balun_ipex": (906, 1649),
    "block.es8311_codec_i2s": (921, 609),
    "block.esp32_autodownload": (821, 21),
    "block.esp32s3_pico_native_usb": (896, 964),
    "block.i2c_isolation_2n7002dw": (846, 41),
    "block.ina226_power_monitor": (831, 309),
    "block.ir_txrx_remote": (846, 581),
    "block.lc29h_dr_gnss_frontend": (911, 886),
    "block.mems_mic_analog": (846, 294),
    "block.microsd_spi_pushpush": (831, 325),
    "block.opto_acc_ign_detect": (831, 297),
    "block.pmos_highside_softstart": (831, 285),
    "block.sdnand_sdmmc_4bit": (856, 574),
    "block.st7789_spi_lcd_btb": (831, 310),
    "block.sy8089_buck_3v3": (838, 299),
    "block.tps63802_buckboost_3v8": (838, 309),
    "block.usbc_dual_orientation_data": (849, 315),
    "block.xl1509_buck_12v_5v": (856, 329),
}
_INK_DEFAULT = (950, 700)  # 未实测 upstream 块保守占位
_CELL_PLACE = (400, 250)  # place 通道单器件符号(保守)
_CALIB_SPACING = 250  # _INK_CELL 的标定格距(A4 实测可整块入图)
_SPACING_DEFAULT = "250"


def _spacing_of(b, spacing_default: int) -> int:
    """块生效格距:per-block params.spacing 优先(P4-1④ RELAYOUT 通道),非法值回退默认。"""
    raw = (b.params or {}).get("spacing", "")
    try:
        return max(int(str(raw).strip()), 100)
    except (TypeError, ValueError):
        return spacing_default


def _cell_for(rec: BlockRecord, spacing: int = _CALIB_SPACING) -> tuple[int, int]:
    """实测占位。upstream 块墨迹随 --spacing 线性缩放(相对 250 标定);
    place 通道符号几何与 spacing 无关(sch place 无该旗标),恒用固定格不缩放。"""
    if rec.upstream is None:
        return _CELL_PLACE[0], _CELL_PLACE[1]
    s = max(int(spacing), 100) / _CALIB_SPACING
    dx, dy = _INK_CELL.get(rec.upstream.id, _INK_DEFAULT)
    return int(dx * s), int(dy * s)


def _spacing_eff(rec: BlockRecord, b, spacing_default: int) -> int:
    """生效格距:params.spacing(RELAYOUT)优先,再按块宽截到 A4 内(x0+dx ≤ 1170)。

    dx 随 spacing 线性放大,不截则 RELAYOUT 反馈给 350+ 会把墨迹静默推出右缘
    (实测最宽 es8311 dx=921:350 → 1289 > 1170);截断只在超宽时收紧,
    不影响 250 标定(全部实测块 250 下右缘 ≤1021)。
    """
    sp = _spacing_of(b, spacing_default)
    if rec.upstream is None:
        return sp  # place 通道无 --spacing 语义,格距只影响流程推进,宽度恒定
    dx, _ = _INK_CELL.get(rec.upstream.id, _INK_DEFAULT)
    ceiling = max(int(_CALIB_SPACING * (_SHEET_W - _GRID_X0) / dx), 100)
    return min(sp, ceiling)


def band_of(rec: BlockRecord, zone_hint: str = "") -> int:
    """块归属带:planner 显式 zone 优先,否则按 category 默认,未知 category 落主控带。"""
    hint = (zone_hint or "").strip().lower()
    if hint in _ZONE_BAND:
        return _ZONE_BAND[hint]
    return _CATEGORY_BAND.get((rec.category or "").strip().lower(), 1)


class _PageFlow:
    """A4 页内纵向堆叠:块按带序(电源→主控→外设)依次入页,累计越 _SHEET_TOP_LIMIT 开新页。

    页名 P1..Pn(P1 = 工程已有首页,controller 对 P2+ 做 page-new/page-rename 并以
    --doc 钉扎)。单块自身超页高(cc1101 类 25 件块)仍占当前页并照常推进——溢出由
    zone-plan/审计暴露(fit-first 不静默),不为巨块硬改几何。
    """

    def __init__(
        self,
        x0: int = _GRID_X0,
        y0: int = _GRID_Y0,
        top_limit: int = _SHEET_TOP_LIMIT,
        gap: int = _STACK_GAP,
    ) -> None:
        self.x0, self.y0, self.top_limit, self.gap = x0, y0, top_limit, gap
        self.page_no = 0
        self.next_y = y0
        self.placed_any = False

    def take(self, dy: int) -> tuple[str, str]:
        """返回 (at, 页名);间隙记在后块(推进量 = dy + gap),换页判定不含间隙。"""
        if self.placed_any and self.next_y + dy > self.top_limit:
            self.page_no += 1
            self.next_y = self.y0
        at = f"{self.x0},{self.next_y}"
        self.next_y += dy + self.gap
        self.placed_any = True
        return at, f"P{self.page_no + 1}"


def compile_actions(
    plan: BlockPlan,
    catalog: dict[str, BlockRecord],
    *,
    spacing_default: str = _SPACING_DEFAULT,
) -> list[Action]:
    plan = _fill_bindings(plan, catalog)
    actions: list[Action] = []
    spacing = int(spacing_default)
    # P4-1①/P4-b2:A4 页流布局。带内大块先放(装箱友好:小块填补页尾)。
    # 产出序 = 流序(页连续升序):--doc 切换粘性,跨页交错产出会让前台来回摆,
    # 也让 P1 动作夹在 P2+ 之后(见 controller._doc_args);流序天然同页聚簇。
    band_blocks: dict[int, list] = {0: [], 1: [], 2: []}
    for b in plan.blocks:
        band_blocks[band_of(catalog[b.block_id], b.zone)].append(b)
    flow = _PageFlow()
    emit: list = []
    for band in (0, 1, 2):
        ordered = sorted(
            band_blocks[band],
            key=lambda b: _cell_for(catalog[b.block_id], _spacing_eff(catalog[b.block_id], b, spacing))[1],
            reverse=True,
        )
        for b in ordered:
            dx, dy = _cell_for(catalog[b.block_id], _spacing_eff(catalog[b.block_id], b, spacing))
            at, page = flow.take(dy)
            b.page = page
            if not b.at:
                b.at = at  # planner/RELAYOUT 显式 at 优先(P4-1④,页内坐标)
            emit.append(b)
    for b in emit:
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
                str(_spacing_eff(catalog[b.block_id], b, spacing)),
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
                    page=b.page,
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
                b.params.get("x", str(_GRID_X0)),
                "--y",
                b.params.get("y", str(_GRID_Y0)),
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
                    page=b.page,
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
                        page=b.page,
                    )
                )
    actions.append(
        Action(kind="sch-gate", block_instance="", upstream_id="", args=["sch", "gate", "--json"], desc="验证门禁")
    )
    return actions
