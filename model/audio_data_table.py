from peewee import IntegerField, TextField

from model.base_mode import BaseModel


class AudioDataTable(BaseModel):
    """
    对应表：audio_data_table

    SQL 里有约束 (sample_rate > 0)；这里先按字段建模，约束由 DB 侧保证。
    """

    audio_data_id = TextField(primary_key=True) # audio_id
    file_path = TextField(unique=True) # 路径
    product_model = TextField() # 产品型号
    sample_rate = IntegerField() # 采样率
    record_date = TextField() # 创建时间
    labels = TextField(null=True) # 标签结果
    label_source = TextField(null=True) # 判定来源：auto/manual
    barcode = TextField(null=True) # SN码
    analyse_config = TextField(null=True) # 分析配置
    config_content = TextField(null=True) # 配置快照

    class Meta:
        table_name = "audio_data_table"

