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

    class Meta:
        table_name = "test_config_table"
