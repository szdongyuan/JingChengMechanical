from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class PlcValueType(Enum):
    BOOL = "bool"
    INT = "int"
    REAL = "real"
    STRING = "string"


class PlcDirection(Enum):
    READ = "read"
    WRITE = "write"
    READ_WRITE = "read_write"


@dataclass(frozen=True)
class PlcFieldSpec:
    key: str
    label: str
    value_type: PlcValueType
    direction: PlcDirection
    required: bool = False
    workorder_key: str | None = None
    workorder_order: int | None = None
    result_role: str | None = None
    result_payload_key: str | None = None
    result_order: int | None = None
    config_return: bool = False


PLC_FIELD_SPECS = (
    PlcFieldSpec(
        "start_signal_offset",
        "启动信号 StartSignal",
        PlcValueType.BOOL,
        PlcDirection.READ_WRITE,
        required=True,
    ),
    PlcFieldSpec(
        "current_value_offset",
        "电流 CurrentValue (REAL/A)",
        PlcValueType.REAL,
        PlcDirection.READ,
    ),
    PlcFieldSpec(
        "rotation_speed_offset",
        "转速值 RotationSpeed",
        PlcValueType.INT,
        PlcDirection.READ,
        workorder_key="rotation_speed",
        workorder_order=30,
    ),
    PlcFieldSpec(
        "res_testing_signal_offset",
        "返回测试中 ResTestingSignal",
        PlcValueType.BOOL,
        PlcDirection.WRITE,
    ),
    PlcFieldSpec(
        "res_finish_signal_offset",
        "返回结束信号 ResFinishSignal",
        PlcValueType.BOOL,
        PlcDirection.WRITE,
        result_role="finish",
    ),
    PlcFieldSpec(
        "res_error_code_offset",
        "返回异常代码 ResErrorCode",
        PlcValueType.INT,
        PlcDirection.WRITE,
        result_role="value",
        result_payload_key="error_code",
        result_order=10,
    ),
    PlcFieldSpec(
        "res_result_offset",
        "返回测试结果 ResResult",
        PlcValueType.BOOL,
        PlcDirection.WRITE,
        result_role="value",
        result_payload_key="test_result",
        result_order=30,
    ),
    PlcFieldSpec(
        "res_score_offset",
        "返回评分 ResScore",
        PlcValueType.REAL,
        PlcDirection.WRITE,
        result_role="value",
        result_payload_key="score",
        result_order=40,
    ),
    PlcFieldSpec(
        "res_spl_offset",
        "返回平均声压级 ResSPL",
        PlcValueType.REAL,
        PlcDirection.WRITE,
        result_role="value",
        result_payload_key="spl",
        result_order=50,
    ),
    PlcFieldSpec(
        "res_loudness_offset",
        "返回响度 ResLoudness",
        PlcValueType.REAL,
        PlcDirection.WRITE,
        result_role="value",
        result_payload_key="loudness",
        result_order=60,
    ),
    PlcFieldSpec(
        "res_vibrate_data_offset",
        "返回中场振动 ResVibrateData",
        PlcValueType.REAL,
        PlcDirection.WRITE,
        result_role="value",
        result_payload_key="vibrate",
        result_order=70,
    ),
    PlcFieldSpec(
        "res_ai_score_offset",
        "返回AI评分 ResAiScore",
        PlcValueType.REAL,
        PlcDirection.WRITE,
        result_role="value",
        result_payload_key="ai_score",
        result_order=80,
    ),
    PlcFieldSpec(
        "res_spl_limit_up_offset",
        "返回声压值上限 ResSplLimitUp",
        PlcValueType.REAL,
        PlcDirection.WRITE,
        config_return=True,
    ),
    PlcFieldSpec(
        "res_spl_limit_down_offset",
        "返回声压值下限 ResSplLimitDown",
        PlcValueType.REAL,
        PlcDirection.WRITE,
        config_return=True,
    ),
    PlcFieldSpec(
        "res_loudness_limit_up_offset",
        "返回响度值上限 ResLoudnessLimitUp",
        PlcValueType.REAL,
        PlcDirection.WRITE,
        config_return=True,
    ),
    PlcFieldSpec(
        "res_loudness_limit_down_offset",
        "返回响度值下限 ResLoudnessLimitDown",
        PlcValueType.REAL,
        PlcDirection.WRITE,
        config_return=True,
    ),
    PlcFieldSpec(
        "res_vibrate_limit_up_offset",
        "返回振动值上限 ResVibrateLimitUp",
        PlcValueType.REAL,
        PlcDirection.WRITE,
        config_return=True,
    ),
    PlcFieldSpec(
        "res_vibrate_limit_down_offset",
        "返回振动值下限 ResVibrateLimitDown",
        PlcValueType.REAL,
        PlcDirection.WRITE,
        config_return=True,
    ),
    PlcFieldSpec(
        "res_ai_score_limit_down_offset",
        "返回AI评分下限 ResAiScoreLimitDown",
        PlcValueType.REAL,
        PlcDirection.WRITE,
        config_return=True,
    ),
    PlcFieldSpec(
        "sn_code_offset",
        "SN码 SnCode",
        PlcValueType.STRING,
        PlcDirection.READ,
        workorder_key="sn",
        workorder_order=10,
    ),
    PlcFieldSpec(
        "product_model_offset",
        "产品型号 ProductModel",
        PlcValueType.STRING,
        PlcDirection.READ,
        workorder_key="model",
        workorder_order=20,
    ),
    PlcFieldSpec(
        "res_sn_code_offset",
        "返回SN码 ResSnCode",
        PlcValueType.STRING,
        PlcDirection.WRITE,
        result_role="value",
        result_payload_key="sn_code",
        result_order=20,
    ),
    PlcFieldSpec(
        "res_product_model_offset",
        "返回机种 ResProductModel",
        PlcValueType.STRING,
        PlcDirection.WRITE,
        config_return=True,
    ),
)

PLC_NEW_RECORD_DEFAULT_OFFSETS: dict[str, str | None] = {
    "start_signal_offset": "0.0",
    "current_value_offset": "2.0",
    "rotation_speed_offset": "6.0",
    "res_testing_signal_offset": "8.0",
    "res_finish_signal_offset": "8.1",
    "res_error_code_offset": "10.0",
    "res_result_offset": "12.0",
    "res_score_offset": "14.0",
    "res_spl_offset": "18.0",
    "res_loudness_offset": "22.0",
    "res_vibrate_data_offset": "26.0",
    "res_ai_score_offset": "30.0",
    "res_spl_limit_up_offset": "34.0",
    "res_spl_limit_down_offset": "38.0",
    "res_loudness_limit_up_offset": "42.0",
    "res_loudness_limit_down_offset": "46.0",
    "res_vibrate_limit_up_offset": "50.0",
    "res_vibrate_limit_down_offset": "54.0",
    "res_ai_score_limit_down_offset": "58.0",
    "sn_code_offset": "62.0",
    "product_model_offset": "80.0",
    "res_sn_code_offset": None,
    "res_product_model_offset": None,
}

PLC_FIELD_BY_KEY = {spec.key: spec for spec in PLC_FIELD_SPECS}
PLC_WORKORDER_FIELDS = tuple(
    sorted(
        (spec for spec in PLC_FIELD_SPECS if spec.workorder_key is not None),
        key=lambda spec: spec.workorder_order,
    )
)
PLC_RESULT_FIELDS = tuple(
    sorted(
        (spec for spec in PLC_FIELD_SPECS if spec.result_role == "value"),
        key=lambda spec: spec.result_order,
    )
)
PLC_CONFIG_RETURN_FIELDS = tuple(
    spec for spec in PLC_FIELD_SPECS if spec.config_return
)


def parse_plc_offset(
    value: str,
    value_type: PlcValueType,
) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)", value)
    if match is None:
        raise ValueError("偏移格式错误，请输入类似 0.0 的格式")

    byte_index, bit_index = (int(part) for part in match.groups())
    if value_type is PlcValueType.BOOL:
        if bit_index > 7:
            raise ValueError("Bool 位偏移必须在 0 到 7 之间")
    elif value_type in (
        PlcValueType.INT,
        PlcValueType.REAL,
        PlcValueType.STRING,
    ):
        if bit_index != 0:
            raise ValueError(f"{value_type.value} 字段必须使用字节对齐的 N.0 偏移")
    else:
        raise ValueError(f"不支持的 PLC 字段类型: {value_type!r}")

    return byte_index, bit_index
