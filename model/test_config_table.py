from peewee import AutoField, BooleanField, FloatField, TextField

from consts.time_consts import now_str
from model.base_mode import BaseModel


class TestConfigTable(BaseModel):
    config_id = AutoField()
    config_name = TextField(unique=True)
    record_duration = FloatField(default=5.0)
    analysis_duration = FloatField(default=3.0)
    scan_triggers_test = BooleanField(default=False)
    created_time = TextField(default=now_str)
    updated_time = TextField(default=now_str)
    trigger_type = TextField(default='manual')
    input_device_name = TextField(null=True)
    input_device_hostapi = TextField(null=True)
    input_device_samplerate = FloatField(null=True)
    output_device_name = TextField(null=True)
    output_device_hostapi = TextField(null=True)
    output_device_samplerate = FloatField(null=True)
    selected_channels = TextField(default='[]')

    class Meta:
        table_name = "test_config_table"
