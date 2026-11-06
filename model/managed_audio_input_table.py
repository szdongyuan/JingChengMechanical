from peewee import (
    AutoField,
    BigIntegerField,
    FloatField,
    IntegerField,
    SQL,
    TextField,
)

from app.service.managed_input_contracts import MAX_CALIBRATABLE_INPUT_CHANNELS
from consts.time_consts import now_str
from model.base_mode import BaseModel


class ManagedAudioInputTable(BaseModel):
    hardware_id = AutoField(constraints=[SQL("AUTOINCREMENT")])
    alias = TextField(unique=True)
    hardware_revision = BigIntegerField(
        default=1,
        constraints=[SQL("CHECK (hardware_revision >= 1)")],
    )
    stable_backend_uid = TextField(null=True, unique=True)
    descriptor_device_name = TextField()
    descriptor_hostapi_name = TextField()
    descriptor_sample_rate_hz = FloatField(
        constraints=[SQL("CHECK (descriptor_sample_rate_hz > 0)")],
    )
    descriptor_input_channels = IntegerField(
        constraints=[
            SQL(
                "CHECK (descriptor_input_channels >= 1 "
                f"AND descriptor_input_channels <= "
                f"{MAX_CALIBRATABLE_INPUT_CHANNELS})"
            )
        ],
    )
    calibration_revision = BigIntegerField(
        default=0,
        constraints=[SQL("CHECK (calibration_revision >= 0)")],
    )
    calibration_json = TextField()
    created_time = TextField(default=now_str)
    updated_time = TextField(default=now_str)

    class Meta:
        table_name = "managed_audio_input_table"
