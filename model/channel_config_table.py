from peewee import AutoField, FloatField, ForeignKeyField, IntegerField, TextField

from model.base_mode import BaseModel
from model.test_config_table import TestConfigTable


class ChannelConfigTable(BaseModel):
    channel_config_id = AutoField()
    config = ForeignKeyField(
        TestConfigTable,
        backref="channels",
        on_delete="CASCADE",
    )
    channel_index = IntegerField()
    stft_nfft = IntegerField(default=1024)
    stft_overlap = FloatField(default=0.5)
    spl_weighting = TextField(default="A")
    spl_time_constant = FloatField(default=0.125)
    spl_upper_limit = FloatField(default=85.0)
    spl_lower_limit = FloatField(default=40.0)
    fft_band_limits = TextField(default="[]")
    oct_weighting = TextField(default="A")
    oct_band_limits = TextField(default="[]")
    ai_config = TextField(default="{}")

    class Meta:
        table_name = "channel_config_table"
        indexes = ((("config", "channel_index"), True),)
