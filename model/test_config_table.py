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
    current_meter_enabled = BooleanField(default=False)
    current_meter_host = TextField(null=True, default=None)
    current_meter_port = IntegerField(default=502)
    current_meter_unit_id = IntegerField(default=1)
    current_meter_register_type = TextField(default="input")
    current_meter_register_address = IntegerField(default=0)
    current_meter_register_count = IntegerField(default=1)
    current_meter_data_type = TextField(default="uint16")
    current_meter_byte_order = TextField(default="big")
    current_meter_word_order = TextField(default="big")
    current_meter_scale = FloatField(default=0.001)
    current_meter_offset = FloatField(default=0.0)
    current_meter_poll_interval_ms = IntegerField(default=200)
    current_meter_timeout_ms = IntegerField(default=1000)

    class Meta:
        table_name = "test_config_table"
