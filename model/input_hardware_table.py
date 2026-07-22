from peewee import AutoField, FloatField, IntegerField, TextField

from consts.time_consts import now_str
from model.base_mode import BaseModel


class InputHardwareTable(BaseModel):
    """One managed physical input device with per-channel calibration data."""

    hardware_id = AutoField()
    alias = TextField(unique=True)
    device_name = TextField()
    hostapi = TextField()
    samplerate = FloatField()
    channel_count = IntegerField()
    mic_cal = TextField(default="{}")
    created_time = TextField(default=now_str)
    updated_time = TextField(default=now_str)

    class Meta:
        table_name = "input_hardware_table"
        indexes = (
            (("device_name", "hostapi", "samplerate", "channel_count"), True),
        )
