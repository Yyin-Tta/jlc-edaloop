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
    from edaloop.generate.stdparts import available_values, kind_of, lookup

    for b in plan.blocks:
        rec = catalog.get(b.block_id)
        if rec is None:
            raise CompileError(f"块 {b.block_id} 不在库中")
        if rec.upstream is not None:
            if b.no_connect:
                raise CompileError(
                    f"块 {b.block_id} 的 upstream 通道不支持 no_connect;"
                    "请使用带 pinout 的 place 器件或补充块级 NC 契约"
                )
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
            std_kind = kind_of(rec)
            if not rec.lcsc and std_kind is None:
                raise CompileError(f"块 {b.block_id} 无 upstream 且无 lcsc(不可落图)")
            if not b.pins_binding and not b.no_connect:
                raise CompileError(
                    f"块 {b.block_id} 是库外器件(place 通道),必须给出 pins_binding(pin号→网络)"
                )
            if rec.pinout:
                unknown = [p for p in set(b.pins_binding) | set(b.no_connect) if p not in rec.pinout]
                if unknown:
                    raise CompileError(f"块 {b.block_id} 绑定了不存在的引脚号: {unknown}")
            overlap = set(b.pins_binding) & set(b.no_connect)
            if overlap:
                raise CompileError(f"块 {b.block_id} 引脚同时绑定网络和 NC: {sorted(overlap)}")
            if std_kind is not None:
                if b.no_connect and any(pin not in {"1", "2"} for pin in b.no_connect):
                    raise CompileError(
                        f"标准件 {b.block_id} 的 NC 引脚号只能是 1/2: {b.no_connect}"
                    )
                # P4-4② std-value 通道:params.value 查标准件表得 lcsc(确定性;表无此值=硬错,
                # 让 planner 显式换值,不静默取最近值)
                if not rec.lcsc:
                    val = (b.params or {}).get("value", "")
                    entry = lookup(std_kind, val) if val else None
                    if entry is None:
                        raise CompileError(
                            f"块 {b.block_id} 的 params.value {val!r} 不在标准件表"
                            f"(可用值: {available_values(std_kind)})"
                        )
    return plan


# A4 横放实测(EasyEDA 单位 = 0.01 inch):1170 × 825,y-UP;图签占位右下 [468..1170, 0..198]。
# 锚点(180,300):x0 由 100→180(2026-08-21 校准 B:首列器件左侧 netport 文字+桩线
# 实测外伸 ~120-150,如 P2 R3 左沿 -48@x0=100),y0 避图签(y≥198)。
_GRID_X0 = 180
_GRID_Y0 = 300
_SHEET_TOP_LIMIT = 800  # 顶部余 25;esp32 类 489 高块 300..789 可整页放下
_SHEET_W = 1170  # A4 横放全宽(墨迹右缘校核用)
_STACK_GAP = 60  # 同页相邻块垂直间隙
# A4 可用区(铁边距 12;顶部 825-12):at 硬界校核用(_validate_at)。
_SHEET_X0, _SHEET_X1, _SHEET_Y0, _SHEET_Y1 = 12, 1158, 12, 801

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
_CALIB_SPACING = 250  # _INK_CELL 的标定格距(A4 实测可整块入图)
_SPACING_DEFAULT = "250"

# ---- 行-货架页流(P5-0/G33 页爆炸修复):place 通道小件行内并排,不再每件独占一页 ----
# 页内流宽:x0(180)+950=1130 ≤ 铁边距 1158;行内 pitch = dx + 2×_WING_PAD_X。
# 宽块(pitch ≥ 流宽)恒独行 → 与旧单列流逐坐标等价,既有页流断言零改动保真。
# 回退开关:_FLOW_W=400 → 除 led_indicator(dx136)外全部独行,近似精确还原单列。
_FLOW_W = 950
_WING_PAD_X = 120  # 行内翼展垫:netport 文字+桩线实测单侧 50~320,取中带保守
# 网名长度维翼展增量(2026-08-24 req-02/06/07 HALT 定案):_PLACE_INK 按 CALNET{pin}
# (≤9 字符)标定、_INK_CELL 的 dx 是器件 bbox 并集根本不含 netport 文字,pitch 的
# pad 120 名义上只覆盖 ~8 字符网;真实网名 max 10-12(USB_5V_RAW/PA8_RS485_DE/
# STEP1_A_OUT 成排),每超 1 字翼展单侧多 15。led_indicator_gpio dx=136+11 字网
# 真实宽 466 > 旧 pitch 376 → 同行相邻 LED 块必叠(req-07 R9×R11 实证)。
_NET_CHAR_W = 15  # netport 每字符净宽(含桩线摊销):R 标定 (261-21)/2/8
_CAL_NET_REF = 8  # 标定参考网长:_PLACE_INK 按 CALNET{pin} 量,_INK_CELL 无翼展但 pad 120≈8 字
# upstream 翼展基线补偿(P2-8,2026-08-26 对齐 _mark_span 真机口径):_INK_CELL 的
# dx 是器件 bbox 并集、完全不含 netport 翼展,pad 120 名义 ≈8 字,但 8 字网单侧
# 真机实测 ≈146(桩 40 + 文字 106,run-885b01f68b1f 的 _mark_span 同口径:
# 12/字符 + 10 下限 60)。基线补 26 只给 upstream;place 实测表按 ≤9 字网连网后
# 量的组盒,翼展已含,只吃长度斜率(斜率 15/字系 req-07 R9×R11 真机缺口反推,
# 3 字缺 45=恰好 15/字,不动的部分)。
_WING_BASE_EXTRA = 26
# 纵向欠账(同日定案,req-01 网名不超 8 仍 HALT):_INK_CELL 的 dy 同为器件 bbox,
# 不含 netport 垂直悬挂(上下各伸 ~30-60),行间只有 _STACK_GAP=60 → autodl dy=21
# 下一行仅 +81 即撞(req-01/06 的 clamp 全是 dy 移动实证)。place 实测 dy 已含
# netport、tier 本就高估,不加(保小件页密度)。
_WING_PAD_Y = 40  # upstream 块 dy 单侧补偿(调参梯:不足→60,页数涨→30)
# place 通道实测墨迹表(2026-08-24 真机标定,证据 .claude/measure-place-ink.json):
# (dx, dy) = 逐 pin autoconnect(netport 已连)后 clusters 组盒——netport 翼展已含在
# dx 里(实测 netport 只加宽不加高:R 符号 21→连网后 261,dy 11)。R/C/STM32 三类
# 以真实引脚名(1/2)复测校正(首轮合成引脚名 A/B 连网失败低估);STM32 的 C8734
# lib-search 无果,走 MPN STM32F103C8T6 命中。表外块走 _PLACE_INK_TIERS 分档。
_PLACE_INK: dict[str, tuple[int, int]] = {
    "resistor-std": (261, 11),
    "capacitor-std": (261, 17),
    "switch-6x6": (281, 13),
    "tvs-smaj5": (261, 16),
    "xtal-8m": (195, 21),
    "fuse-polyfuse": (301, 11),
    "diode-ss34": (261, 17),
    "terminal-kf301-2p": (176, 31),
    "nmos-2n7002": (251, 51),
    "pmos-ao3401": (251, 51),
    "isolator-pc817": (311, 35),
    "isolated-dc-b0505s": (226, 51),
    "header-1x4": (331, 81),
    "esd-usblc6": (255, 41),
    "usb-serial-ch340k": (497, 110),
    "mcu-stm32f103c8-min": (593, 291),
}
_PLACE_INK_TIERS: tuple[tuple[int, tuple[int, int]], ...] = (
    (2, (400, 250)),  # 2 脚小件(R/C/二极管类)= 旧 _CELL_PLACE
    (9, (450, 350)),  # 3-9 脚中件(晶体管/MOS/连接器类)
    (1 << 30, (550, 500)),  # 10+ 脚大件(SOP/连接器排类)
)
_CELL_PLACE = (400, 250)  # place 通道兜底单格(_PLACE_INK_TIERS 档 1 同源)

# ⚠ _INK_CELL 的 dx/dy 是「器件 bbox 并集」,**不含 netport/netflag 文字翼展**
# (2026-08-21 校准 A/B 实测:标签+桩线在 bbox 外单侧可伸 50~320 —— 长 --instance
# 命名的内部网(USBC_ENTRY_N1)曾把 J1 类连接器翼展顶到 390;去掉 --instance 走
# 默认位号短名(D1_N1)后回落到 ~120-230,边界网名 RS485_A 类仍可达 ~320)。
# 翼展不进 _spacing_eff 的宽度截断(末列右翼实测小,截了反而毁标定几何),
# 只体现在:x0(左翼)、_validate_at(硬界)与 P4-b3 组排布(列间翼展撞邻列)。

# per-block 实测锚点(2026-08-21 校准 B 逐页 clusters --strict 验证):块 → (x0, spacing)。
# 表内值整组实测落位(翼展已含在位里),_spacing_eff 对表内块不做宽度截断。
# rs485:U4 收发器左翼 322(RS485_A/B 文字)→ x0=340;9 件三行块需 dy≤498 → sp=210
# (250 时第三行 U4 顶部溢出 A4)。证据:run/calib/P5-apply.json + clusters 输出。
_BLOCK_LAYOUT: dict[str, tuple[int, int]] = {
    "block.sp3485_rs485_halfduplex": (340, 210),
}


def _spacing_of(rec: BlockRecord, b, spacing_default: int) -> int:
    """块生效格距:params.spacing(P4-1④ RELAYOUT 通道)优先,再 _BLOCK_LAYOUT 实测
    锚点,最后默认;非法值视同缺省。"""
    raw = (b.params or {}).get("spacing", "")
    try:
        return max(int(str(raw).strip()), 100)
    except (TypeError, ValueError):
        pass
    lay = _BLOCK_LAYOUT.get(rec.upstream.id) if rec.upstream else None
    if lay:
        return lay[1]
    return spacing_default


def _place_cell(rec: BlockRecord) -> tuple[int, int]:
    """place 通道占位:_PLACE_INK 实测墨迹优先(逐 pin autoconnect 后组盒,
    netport 翼展已含);未标定按引脚数分档保守缺省。与 spacing 无关
    (sch place 无该旗标)。"""
    ink = _PLACE_INK.get(rec.block_id)
    if ink:
        return ink[0], ink[1]
    n = len(rec.pinout or {})
    for cap, cell in _PLACE_INK_TIERS:
        if n <= cap:
            return cell
    return _CELL_PLACE


def _cell_for(rec: BlockRecord, spacing: int = _CALIB_SPACING) -> tuple[int, int]:
    """实测占位。upstream 块墨迹随 --spacing 线性缩放(相对 250 标定);
    place 通道符号几何与 spacing 无关(sch place 无该旗标),恒用固定格不缩放。"""
    if rec.upstream is None:
        return _place_cell(rec)
    s = max(int(spacing), 100) / _CALIB_SPACING
    dx, dy = _INK_CELL.get(rec.upstream.id, _INK_DEFAULT)
    return int(dx * s), int(dy * s)


def _wing_extra(rec: BlockRecord, b) -> int:
    """网名长度维的翼展单侧增量:_CAL_NET_REF 字符以内按标定口径已覆盖
    (place 实测墨迹含 CALNET{pin} 翼展;upstream bbox 无翼展但 pad 120 名义
    ≈8 字),超出部分按每字符 _NET_CHAR_W 补。upstream 额外加基线
    _WING_BASE_EXTRA:bbox 完全无翼展,pad 120 对 8 字网实测缺 26。
    取该块所绑最长网名(pins_binding/ports_binding 并集,两通道在
    _fill_bindings 后都已填)。"""
    nets = set(b.pins_binding.values()) | set(b.ports_binding.values())
    longest = max((len(n) for n in nets), default=0)
    extra = _NET_CHAR_W * max(0, longest - _CAL_NET_REF)
    if rec.upstream is not None:
        extra += _WING_BASE_EXTRA
    return extra


def _spacing_eff(rec: BlockRecord, b, spacing_default: int) -> int:
    """生效格距:params.spacing(RELAYOUT)优先,再按块宽/块高截到 A4 内。

    dx 随 spacing 线性放大,不截则 RELAYOUT 反馈给 350+ 会把墨迹静默推出右缘
    (实测最宽 es8311 dx=921:350 → 1289 > 1170);截断只在超宽时收紧,
    不影响 250 标定(全部实测块 250 下右缘 ≤1021)。
    dy 同理纵向截断(2026-08-24 req-06 定案):esp32s3_pico_native_usb
    dy=964 > A4 可用高 500,sp=250 下子件落 y~1264 出界 805,钳移回带又与
    邻件相挤成 rc=1 死锁——sp 压到 ⌊250×500/964⌋=129 后 dy=497 可整块入页;
    cc1101 类(1649)压到下限 100 仍放不下的维持 fit-first 照放(审计暴露)。
    _BLOCK_LAYOUT 实测锚点块免截断:整组几何是逐页 clusters 验证过的,
    翼展截断反而会破坏标定(如 rs485 sp=210 的三行行距)。
    """
    sp = _spacing_of(rec, b, spacing_default)
    if rec.upstream is None:
        return sp  # place 通道无 --spacing 语义,格距只影响流程推进,宽度恒定
    if rec.upstream.id in _BLOCK_LAYOUT:
        return sp
    dx, dy = _INK_CELL.get(rec.upstream.id, _INK_DEFAULT)
    ceiling_w = max(int(_CALIB_SPACING * (_SHEET_W - _GRID_X0) / dx), 100)
    ceiling_h = max(int(_CALIB_SPACING * (_SHEET_TOP_LIMIT - _GRID_Y0) / dy), 100)
    return min(sp, ceiling_w, ceiling_h)


def _validate_at(
    at: str, dx: int, dy: int, fallback: str,
    placed: list[tuple[int, int, int, int]] | None = None,
) -> str:
    """planner/RELAYOUT 显式 at 做 A4 硬界 + 邻块碰撞校核,任一不过回退流式位
    (fit-first 不静默)。

    硬界拦「整块飞出图纸」级灾难(run4 r2 实例:RELAYOUT 给 rs485 at=950,480,
    4 列×336 步长=2294,整块飞出 A4 右缘)。碰撞拦「显式 at 压进已落块的 ink 框」
    (P1-7,2026-08-26):流式位由 pitch/行进数学保证不叠,显式位此前无任何邻块
    检查——RELAYOUT 反馈给的 at 落在别块 cell 上即真叠。cell 已含翼展估算,
    相交即拒,双侧各放 20 余量。列间 netport 翼展擦撞的精度问题仍归 P4-b3
    组排布层,不在此假装能算准(翼展实测 50~320 且逐 pin 而异)。
    右侧留 100:末列右翼 + 少量文本;左侧 130 / 下 60:首列/末行外侧标签桩线。
    """
    try:
        x_s, y_s = at.split(",")
        x, y = int(x_s), int(y_s)
    except ValueError:
        return fallback
    if x < 130 or y < 60 or x + dx > _SHEET_X1 - 100 or y + dy > _SHEET_Y1:
        return fallback
    for (px, py, pw, ph) in placed or ():
        if (x - 20 < px + pw + 20 and x + dx + 20 > px - 20
                and y - 20 < py + ph + 20 and y + dy + 20 > py - 20):
            return fallback
    return at


def band_of(rec: BlockRecord, zone_hint: str = "") -> int:
    """块归属带:planner 显式 zone 优先,否则按 category 默认,未知 category 落主控带。"""
    hint = (zone_hint or "").strip().lower()
    if hint in _ZONE_BAND:
        return _ZONE_BAND[hint]
    return _CATEGORY_BAND.get((rec.category or "").strip().lower(), 1)


class _PageFlow:
    """A4 页内行-货架流:块按带序(电源→主控→外设)入页;行内左→右铺
    (pitch = dx + 2×_WING_PAD_X),行宽尽换行(行进 = 行内最大 dy + gap),
    页高尽换页。宽块(pitch ≥ 流宽)恒独行 → 与旧单列流逐坐标等价。

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
        flow_w: int = _FLOW_W,
    ) -> None:
        self.x0, self.y0, self.top_limit, self.gap, self.flow_w = x0, y0, top_limit, gap, flow_w
        self.page_no = 0
        self.next_y = y0
        self.row_x = x0
        self.row_dy = 0
        self.placed_any = False

    def _close_row(self) -> None:
        self.next_y += self.row_dy + self.gap
        self.row_x = self.x0
        self.row_dy = 0

    def break_row(self) -> None:
        """强制封行(带边界/锚点块):已开的行封口,下一块从行首起。"""
        if self.row_x > self.x0:
            self._close_row()

    def take(self, dx: int, dy: int) -> tuple[str, str]:
        """返回 (at, 页名)。行宽尽换行、页高尽换页(判定不含间隙);
        间隙记在后块(行进含 gap),同旧单列语义。"""
        pitch = dx + 2 * _WING_PAD_X
        if self.row_x > self.x0 and self.row_x + pitch > self.x0 + self.flow_w:
            self._close_row()
        if self.placed_any and self.next_y + dy > self.top_limit:
            self.page_no += 1
            self.next_y = self.y0
            self.row_x = self.x0
            self.row_dy = 0
        at = f"{self.row_x},{self.next_y}"
        self.row_x += pitch
        self.row_dy = max(self.row_dy, dy)
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

    def _dy_eff(b) -> int:
        # upstream 墨迹 dy 不含 netport 垂直悬挂,行距按 dy+2×pad 推进;
        # place 实测 dy 已含 netport,tier 本就高估——不加,保小件页密度。
        _, dy = _cell_for(catalog[b.block_id], _spacing_eff(catalog[b.block_id], b, spacing))
        return dy + 2 * _WING_PAD_Y if catalog[b.block_id].upstream is not None else dy

    # 已落块 cell 登记(P1-7),**按页分桶**:显式 at 与后续流式位共用同一张
    # 占用表——显式位校核邻块碰撞,流式位反过来避开先前的显式位(显式块不占
    # 流游标,它的实际落点必须登记,否则后续流式块可能直接铺到显式块身上)。
    # 分页是因为流式换页后回到同页首锚点,跨页比较会把前一页首块误判成碰撞。
    placed_by_page: dict[str, list[tuple[int, int, int, int]]] = {}

    def _take_checked(dx_eff: int, dy_eff: int) -> tuple[str, str]:
        """流式取位 + 同页占用表避让:落点撞已登记 cell(显式 at)则封行独占新行。"""
        at, page = flow.take(dx_eff, dy_eff)
        placed = placed_by_page.get(page)
        if placed:
            x, y = (int(v) for v in at.split(",")[:2])
            hit = any(
                x - 20 < px + pw + 20 and x + dx_eff + 20 > px - 20
                and y - 20 < py + ph + 20 and y + dy_eff + 20 > py - 20
                for (px, py, pw, ph) in placed
            )
            if hit:
                flow.break_row()
                at, page = flow.take(dx_eff, dy_eff)
        return at, page

    for band in (0, 1, 2):
        flow.break_row()  # 带不共行(页内行段=带,分区语义保真)
        ordered = sorted(band_blocks[band], key=_dy_eff, reverse=True)
        for b in ordered:
            rec = catalog[b.block_id]
            dx, dy = _cell_for(rec, _spacing_eff(rec, b, spacing))
            dx_eff = dx + 2 * _wing_extra(rec, b)
            dy_eff = dy + 2 * _WING_PAD_Y if rec.upstream is not None else dy
            lay = _BLOCK_LAYOUT.get(rec.upstream.id) if rec.upstream else None
            if lay:
                flow.break_row()  # 锚点块独占行(整组实测几何,不与邻件拼行)
            at, page = _take_checked(dx_eff, dy_eff)
            if lay:
                # 实测锚点 x0(块左翼已量入,如 rs485 U4 的 RS485_A/B 文字翼 322)
                at = f"{lay[0]},{at.split(',')[1]}"
            b.page = page
            if not b.at:
                b.at = at  # planner/RELAYOUT 显式 at 优先(P4-1④,页内坐标)
            else:
                b.at = _validate_at(b.at, dx_eff, dy_eff, at, placed=placed_by_page.get(page))
            try:
                _px, _py = (int(v) for v in b.at.split(",")[:2])
                placed_by_page.setdefault(page, []).append((_px, _py, dx_eff, dy_eff))
            except ValueError:
                pass
            emit.append(b)
    for b in emit:
        rec = catalog[b.block_id]
        band = band_of(rec, b.zone)
        if rec.upstream is not None:
            # 不传 --instance(2026-08-21 校准 B 根因):instance 名会进内部网名,
            # 长 instance(usbc_entry)→ USBC_ENTRY_N1 类 13 字符 netport 文字把
            # 连接器翼展顶到 390 单位(J1↔邻列必撞);默认用首个位号命名(D1_N1,
            # 5 字符)翼展回落 ~120-230。审计追踪走 Action.block_instance,不依赖它。
            args = [
                "sch",
                "block-apply",
                b.upstream_id,
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
            # P4-4② std-value 通道:值查表得 lcsc/mpn(resistor-std/capacitor-std);
            # 普通库外器件仍走 rec.lcsc(_fill_bindings 已保证其一存在)
            from edaloop.generate.stdparts import kind_of, lookup

            std_kind = kind_of(rec)
            place_lcsc = rec.lcsc or ""
            place_mpn = (rec.parts[0].ref if rec.parts else "")
            if std_kind is not None and not rec.lcsc:
                entry = lookup(std_kind, (b.params or {}).get("value", ""))
                place_lcsc = (entry or {}).get("lcsc", "")
                place_mpn = (entry or {}).get("mpn") or place_mpn
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
                    lcsc=place_lcsc,
                    mpn=place_mpn,
                    args=["lib", "search", "--query", place_lcsc, "--limit", "3"],
                    desc=f"查 {place_lcsc} 的库 uuid(place 前置,C 号无映射时回退 MPN)",
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
            # --pin 用引脚号(pinout 键)不用名字:连接器重名脚(USB-C 16P 的
            # VBUS/GND/EP 各 2-4 个)按名解析撞 ambiguous 直接 rc≠0(req-07 实证);
            # pin-verify 证明符号引脚号与目录键一致,号维永远唯一。
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
                            f"{designator}:{pin}",
                            "--kind",
                            kind,
                            "--net",
                            net,
                        ],
                        desc=f"{designator}:{pin}({pin_name}) -> {net}",
                        page=b.page,
                    )
                )
            if b.no_connect:
                actions.append(
                    Action(
                        kind="sch-no-connect",
                        block_instance=b.instance,
                        args=[
                            "sch", "no-connect", "--designator", designator,
                            "--pin", ",".join(b.no_connect),
                        ],
                        desc=f"{designator}: NC {','.join(b.no_connect)}",
                        page=b.page,
                    )
                )
    actions.append(
        Action(kind="sch-gate", block_instance="", upstream_id="", args=["sch", "gate", "--json"], desc="验证门禁")
    )
    return actions


def ink_cells(
    plan: BlockPlan,
    catalog: dict[str, BlockRecord],
    *,
    spacing_default: str = _SPACING_DEFAULT,
) -> dict[str, tuple[int, int, int, str]]:
    """块实例 -> (dx, dy, band_id, kind) 估算 cell(repack 装箱输入)。

    upstream 块仅在试放回读缺失时兜底(实测框优先——真机 clusters 的 box 含
    netport 文字墨迹,估算永远追不上);place 块恒用估算(其 netport 在逐 pin
    autoconnect 后才存在,试放成本=正式落图,不合算)。dx 含 _wing_extra、
    upstream dy 含 _WING_PAD_Y,与 compile_actions 布局段同口径。
    """
    plan = _fill_bindings(plan, catalog)
    spacing = int(spacing_default)
    out: dict[str, tuple[int, int, int, str]] = {}
    for b in plan.blocks:
        rec = catalog[b.block_id]
        dx, dy = _cell_for(rec, _spacing_eff(rec, b, spacing))
        # FRM-5(v0.6.11 审计):超 _CAL_NET_REF 的长网字符斜率两通道同加
        # (compile_actions 布局段 dx_eff 同口径,流式装箱两套口径曾不一致
        # → repack 低估 place 块 → 装箱重叠);_WING_PAD_Y 仍 upstream 专属
        # (place 实测 dy 已含 netport 悬挂,_dy_eff 同判)。
        dx += 2 * _wing_extra(rec, b)
        if rec.upstream is not None:
            dy += 2 * _WING_PAD_Y
        out[b.instance] = (
            int(dx),
            int(dy),
            band_of(rec, b.zone),
            "upstream" if rec.upstream is not None else "place",
        )
    return out
