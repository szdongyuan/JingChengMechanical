from peewee import (
    AutoField,
    BooleanField,
    FloatField,
    ForeignKeyField,
    IntegerField,
    TextField,
)

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
    fft_nfft = IntegerField(default=4096)
    stft_overlap = FloatField(default=0.5)
    stft_color_scale_enabled = BooleanField(default=False)
    stft_color_scale_min_db = FloatField(default=-120.0)
    stft_color_scale_max_db = FloatField(default=0.0)
    stft_score_enabled = BooleanField(default=True)
    stft_score_summary_mode = TextField(default="percentile")
    stft_score_percentile = FloatField(default=10.0)
    stft_score_loudness_reference_sone = FloatField(default=10.0)
    stft_score_modulation_reference = FloatField(default=0.20)
    stft_score_harmonic_ratio_reference = FloatField(default=0.25)
    stft_score_frequency_instability_reference = FloatField(default=0.05)
    stft_score_harmonic_start_order = IntegerField(default=2)
    stft_score_harmonic_end_order = IntegerField(default=100)
    spl_weighting = TextField(default="A")
    spl_time_constant = FloatField(default=0.125)
    spl_lower_enabled = BooleanField(default=False)
    spl_upper_enabled = BooleanField(default=False)
    spl_upper_limit = FloatField(default=85.0)
    spl_lower_limit = FloatField(default=40.0)
    spl_average_lower_enabled = BooleanField(default=False)
    spl_average_lower_limit = FloatField(default=40.0)
    spl_average_upper_enabled = BooleanField(default=False)
    spl_average_upper_limit = FloatField(default=85.0)
    loudness_unit = TextField(default="sone")
    loudness_lower_enabled = BooleanField(default=False)
    loudness_upper_enabled = BooleanField(default=False)
    loudness_lower_limit = FloatField(default=0.0)
    loudness_upper_limit = FloatField(default=60.0)
    loudness_average_lower_enabled = BooleanField(default=False)
    loudness_average_lower_limit = FloatField(default=0.0)
    loudness_average_upper_enabled = BooleanField(default=False)
    loudness_average_upper_limit = FloatField(default=60.0)
    fft_x_axis_scale = TextField(default="linear")
    fft_band_limits = TextField(default="[]")
    oct_weighting = TextField(default="A")
    oct_band_limits = TextField(default="[]")
    ai_config = TextField(default="{}")
    golden_sample_path = TextField(null=True, default=None)
    golden_sample_label = TextField(null=True, default=None)
    golden_sample_source_channel = IntegerField(null=True, default=None)
    golden_sample_start_sample = IntegerField(null=True, default=None)
    golden_sample_num_samples = IntegerField(null=True, default=None)
    golden_sample_start_ms = IntegerField(null=True, default=None)
    golden_sample_duration_ms = IntegerField(null=True, default=None)
    golden_sample_sample_rate = FloatField(null=True, default=None)
    fft_golden_mode = TextField(default="off")
    fft_show_golden_overlay = BooleanField(default=False)
    oct_golden_mode = TextField(default="off")
    oct_show_golden_overlay = BooleanField(default=False)

    class Meta:
        table_name = "channel_config_table"
        indexes = ((("config", "channel_index"), True),)
