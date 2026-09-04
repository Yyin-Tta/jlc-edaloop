"""历史 PASS 基线的 FUNC_UNCOVERED 评测口径。"""

from edaloop.evals_refine import _historical_func_refs


def test_historical_func_refs_reads_round_validate_parallel_arrays() -> None:
    events = [
        {
            "kind": "round-validate",
            "weak_codes": ["IR_UNCOVERED", "FUNC_UNCOVERED"],
            "weak": [
                "一个普通缺口",
                "IR 功能「备用 5V 输入」在计划 3 块的词表/词元匹配中无覆盖证据",
            ],
        }
    ]
    assert _historical_func_refs(events) == {"备用 5V 输入"}


def test_historical_func_refs_supports_legacy_finding_text() -> None:
    events = [
        {
            "kind": "round-validate",
            "weak": [
                "code='FUNC_UNCOVERED' where=Where(ref='门磁检测', net='', pin='', xy='') evidence='旧格式'"
            ],
        }
    ]
    assert _historical_func_refs(events) == {"门磁检测"}

