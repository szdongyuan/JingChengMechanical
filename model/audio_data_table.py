from peewee import IntegerField, TextField

from model.base_mode import BaseModel


class AudioDataTable(BaseModel):
    """
    对应表：audio_data_table

    SQL 里有约束 (sample_rate > 0)；这里先按字段建模，约束由 DB 侧保证。
    """

    audio_data_id = TextField(primary_key=True)
    file_path = TextField(unique=True)
    product_model = TextField()
    sample_rate = IntegerField()
    record_date = TextField()
    labels = TextField(null=True)
    barcode = TextField(null=True)

    class Meta:
        table_name = "audio_data_table"

