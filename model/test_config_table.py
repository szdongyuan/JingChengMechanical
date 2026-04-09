from peewee import AutoField, BooleanField, FloatField, IntegerField, TextField

from consts.time_consts import now_str
from model.base_mode import BaseModel


class TestConfigTable(BaseModel):
    config_id = AutoField()
    config_name = TextField(unique=True)
    record_duration = FloatField(default=5.0)
    analysis_duration = FloatField(default=3.0)
    allow_incomplete_recording_analysis = BooleanField(default=False)
    scan_triggers_test = BooleanField(default=False)
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
    analysis_window_offset_ms = IntegerField(default=0)

    class Meta:
        table_name = "test_config_table"
