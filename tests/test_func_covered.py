# P4-5② check_func_covered 专属测试:五轮 eval 调参沉淀的行为契约。
# 每条用例都对应一次实跑翻车(见 DEVELOPMENT.md v0.6.7),别轻易放宽。
from edaloop.generate.models import BlockPlan, PlannedBlock
from edaloop.intent.ir import DesignIR
from edaloop.knowledge.models import BlockRecord
from edaloop.validate.checks import check_func_covered


def _rec(block_id: str, name: str, desc: str = "", category: str = "general", tags=None):
    return BlockRecord(
        block_id=block_id, name=name, desc=desc, category=category, tags=tags or [], ports=["A", "B"]
    )


def _plan(*block_ids: str) -> BlockPlan:
    return BlockPlan(blocks=[PlannedBlock(block_id=b, instance=b) for b in block_ids])


def _ir(*names_descs: tuple[str, str]) -> DesignIR:
    return DesignIR.model_validate(
        {"source": "t", "functions": [{"name": n, "desc": d} for n, d in names_descs]}
    )


def _flagged(ir: DesignIR, plan: BlockPlan, catalog=None) -> set[str]:
    return {f.where.ref for f in check_func_covered(ir, plan, catalog)}


def test_func_uncovered_always_weak() -> None:
    """恒弱:PASS run 永不被启发式对齐杀掉(weak + severity=warn 双保险)。"""
    ir = _ir(("主控", "STM32 最小系统"))
    flags = check_func_covered(ir, _plan("led-indicator"), {})
    assert len(flags) == 1
    f = flags[0]
    assert f.code == "FUNC_UNCOVERED" and f.weak is True and f.severity == "warn"


def test_mcu_support_does_not_cover_mcu() -> None:
    """mcu-support 是「服务于 MCU 的电路」不是 MCU 本体——碎片词元 mcu 不得覆盖主控。

    实跑翻车:req-01 注入 drop mcu 块后,「MCU 主控」仍被 up-esp32_autodownload
    (category=mcu-support)的碎片 mcu 覆盖,注入漏报。
    """
    cat = {
        "up-autodl": _rec("up-autodl", "ESP32 双三极管自动下载", category="mcu-support",
                          tags=["autodownload", "esp32"]),
        "mcu-esp32s3": _rec("mcu-esp32s3", "ESP32-S3-WROOM-1 最小系统", category="mcu",
                            tags=["esp32-s3", "mcu", "wifi"]),
    }
    ir = _ir(("MCU 主控", "ESP32-S3 最小系统"))
    assert "MCU 主控" in _flagged(ir, _plan("up-autodl"), cat)          # 只有支持电路 → 漏
    assert "MCU 主控" not in _flagged(ir, _plan("mcu-esp32s3"), cat)    # 真 mcu 块 → 覆盖


def test_pseudo_instance_declares_carrier() -> None:
    """伪块实例名(mcu_main)是 planner 的承载声明,前缀命中算覆盖。

    实跑翻车:标签改连字符整词后,req-09 的自由拓扑实例 mcu_main 不再覆盖「主控」,误伤。
    """
    ir = _ir(("主控", "STM32F103C8T6 主控,采集一路模拟传感器"))
    assert "主控" not in _flagged(ir, _plan("mcu_main"), {})


def test_desc_generic_words_do_not_cover() -> None:
    """name 优先:desc 泛化词(5V/锂电)不救缺口——升压块 desc 提「锂电 3.7v」
    不覆盖「锂电池充电」(req-05 注入实跑翻车);CJK bigram 只认标签字段。"""
    cat = {
        "boost": _rec("boost-mt3608", "MT3608 升压", desc="锂电 3.7V 升到 5V/1A 输出",
                      category="power", tags=["升压", "boost"]),
    }
    ir = _ir(("锂电池充电", "锂电池充电管理,输入 5V"))
    assert "锂电池充电" in _flagged(ir, _plan("boost-mt3608"), cat)
    cat["charger"] = _rec("charger-tp4056", "TP4056 单节锂电池充电", category="power",
                          tags=["充电", "tp4056", "锂电池"])
    assert "锂电池充电" not in _flagged(ir, _plan("charger-tp4056"), cat)


def test_specific_rescue_only_from_pseudo_instance() -> None:
    """专名救援只认伪块实例名:alarm_tl431 直证、do_uln ↔ uln2003 反向前缀都算;
    目录块 name 里的型号碎片不算(up-esp32_autodownload 的 esp32 救不了主控)。"""
    ir_lowvolt = _ir(("低压告警", "电源低于阈值时 TL431 触发告警"))
    assert "低压告警" not in _flagged(ir_lowvolt, _plan("alarm_tl431"), {})

    ir_do = _ir(("数字输出驱动", "ULN2003 达林顿驱动 7 路数字输出"))
    assert "数字输出驱动" not in _flagged(ir_do, _plan("do_uln"), {})

    cat = {"up-autodl": _rec("up-autodl", "ESP32 双三极管自动下载", category="mcu-support",
                             tags=["esp32", "autodownload"])}
    ir_mcu = _ir(("MCU 主控", "ESP32-S3 最小系统"))
    assert "MCU 主控" in _flagged(ir_mcu, _plan("up-autodl"), cat)


def test_short_ascii_token_whole_word_only() -> None:
    """协议专名功能(RS-485 收发)不被泛化射频块覆盖:短 ascii 词元只认标签整词,
    不做全文子串——rf 这类 2 字符右词曾在任意长词里诈胡(req-02 注入实跑翻车)。"""
    cat = {
        "rf-mash": _rec("rf-mash", "射频前端", desc="sub-GHz RF 收发,支持 BLE 广播",
                        category="rf", tags=["rf", "sub-ghz"]),
    }
    ir = _ir(("RS-485 收发", "半双工 RS-485 总线,MAX485 电平转换"))
    assert "RS-485 收发" in _flagged(ir, _plan("rf-mash"), cat)
    cat["rs485"] = _rec("rs485-max485", "MAX485 RS-485 收发", category="comms",
                        tags=["rs-485", "max485", "485"])
    assert "RS-485 收发" not in _flagged(ir, _plan("rs485-max485"), cat)


def test_cjk_bigram_label_only_and_synonym_bridge() -> None:
    """CJK bigram 只对标签字段;同义表桥(测试点↔testpoint)让标签词命中功能名。"""
    cat = {
        "tp": _rec("testpoint-gold", "镀金测试点", category="debug", tags=["testpoint", "测试点"]),
        "mount": _rec("mount-m3", "M3 安装孔", category="mechanical", tags=["安装孔", "mount"]),
    }
    ir = _ir(("测试点", "预留 3V3/GND 测试点"), ("结构固定", "四角 M3 螺丝安装孔"))
    assert _flagged(ir, _plan(), cat) == {"测试点", "结构固定"}
    assert "测试点" not in _flagged(ir, _plan("testpoint-gold"), cat)
    assert "结构固定" not in _flagged(ir, _plan("mount-m3"), cat)
