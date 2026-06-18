from peewee import AutoField, BigIntegerField, FloatField, IntegerField, TextField

from model.base_mode import BaseModel


class HistoricalMetricCacheTable(BaseModel):
    cache_id = AutoField()
    audio_data_id = TextField(index=True)
    file_path = TextField()
    file_size = BigIntegerField()
    file_mtime_ns = BigIntegerField()
    record_date = TextField()
    product_model = TextField()
    barcode = TextField(null=True)
    label = TextField(null=True)
    label_source = TextField(null=True)
    analyse_config = TextField(null=True)
    config_hash = TextField()
    calibration_hash = TextField()
    algorithm_version = TextField()
    channel_index = IntegerField()
    channel_label = TextField()
    metric_key = TextField()
    metric_label = TextField()
    metric_value = FloatField(null=True)
    metric_unit = TextField()
    status = TextField()
    error_message = TextField(null=True)
    computed_at = TextField()

    class Meta:
        table_name = "historical_metric_cache_table"
        indexes = (
            (
                (
                    "audio_data_id",
                    "config_hash",
                    "algorithm_version",
                    "channel_index",
                    "metric_key",
                ),
                True,
            ),
            (("product_model", "record_date"), False),
            (("metric_key", "status"), False),
        )
