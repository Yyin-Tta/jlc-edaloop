from __future__ import annotations

from edaloop.intent.ir import DesignIR


def test_spec_numeric_value_coerced_to_str() -> None:
    """parse LLM 偶发吐 JSON 数值(daily req-03 实锤 value=5 int)→ 裹胁成 "5",不再 ValidationError。"""
    ir = DesignIR.model_validate(
        {"source": "t.md", "functions": [{"name": "f", "constraints": [{"param": "供电", "value": 5, "unit": "V"}]}]}
    )
    assert ir.functions[0].constraints[0].value == "5"


def test_spec_numeric_float_and_tolerance() -> None:
    ir = DesignIR.model_validate(
        {
            "source": "t.md",
            "functions": [
                {"name": "f", "constraints": [{"param": "电压", "value": 3.30, "unit": "V", "tolerance": 2}]}
            ],
        }
    )
    c = ir.functions[0].constraints[0]
    assert c.value == "3.3" and c.tolerance == "2"


def test_spec_bool_none_passthrough() -> None:
    """bool/None 不裹胁(None 走既有 Optional 校验,bool 会 ValidationError——本就该严)。"""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DesignIR.model_validate(
            {"source": "t.md", "functions": [{"name": "f", "constraints": [{"param": "x", "value": True}]}]}
        )


def test_spec_str_unchanged() -> None:
    ir = DesignIR.model_validate(
        {"source": "t.md", "functions": [{"name": "f", "constraints": [{"param": "供电", "value": "5V"}]}]}
    )
    assert ir.functions[0].constraints[0].value == "5V"
