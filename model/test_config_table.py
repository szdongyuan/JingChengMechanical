from peewee import (
    AutoField,
    BooleanField,
    FloatField,
    ForeignKeyField,
    IntegerField,
    TextField,
)

from consts.time_consts import now_str
from model.base_mode import BaseModel
from model.plc_table import PlcTable


class TestConfigTable(BaseModel):
    config_id = AutoField()
    config_name = TextField(unique=True)
    record_duration = FloatField(default=5.0)
    analysis_duration = FloatField(default=3.0)
    allow_incomplete_recording_analysis = BooleanField(default=False)
    scan_triggers_test = BooleanField(default=False)
    plc = ForeignKeyField(
        PlcTable,
        backref="configs",
        null=True,
        default=None,
        on_delete="SET NULL",
        column_name="plc_id",
    )
    plc_device_key = TextField(null=True, default=None)
    plc_device_label = TextField(null=True, default=None)
    created_time = TextField(default=now_str)
    updated_time = TextField(default=now_str)
    input_device_name = TextField(null=True)
    input_device_hostapi = TextField(null=True)
    input_device_samplerate = FloatField(null=True)
    output_device_name = TextField(null=True)
    output_device_hostapi = TextField(null=True)
    output_device_samplerate = FloatField(null=True)
    selected_channels = TextField(default='[]')
    vibration_view_channel_index = IntegerField(null=True, default=None)
    analysis_window_offset_ms = IntegerField(default=0)
    monitor_channel_index = IntegerField(null=True, default=None)
    monitor_gain_db = FloatField(null=True, default=None)

    class Meta:
        table_name = "test_config_table"
