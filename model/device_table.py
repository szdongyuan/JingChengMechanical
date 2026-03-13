from peewee import Check, IntegerField, TextField

from model.base_mode import BaseModel


class DeviceTable(BaseModel):
    """
    对应表：device_table

    你要“只能有 1 条数据”，最稳的是数据库侧约束：id=1 + PRIMARY KEY。
    这里也加一个 Check 约束（Peewee 层面的建表时会带上；若表已存在则以 DB 为准）。
    """

    id = IntegerField(primary_key=True, constraints=[Check("id = 1")])

    input_selected_device_id = IntegerField(null=True)
    input_selected_device_name = TextField(null=True)
    input_selected_device_type = TextField(null=True)
    input_selected_device_samplerate = TextField(null=True)
    input_channel_count = TextField(null=True)

    output_selected_device_id = IntegerField(null=True)
    output_selected_device_name = TextField(null=True)
    output_selected_device_type = TextField(null=True)
    output_selected_device_samplerate = TextField(null=True)
    output_channel_count = TextField(null=True)
    mic_cal = TextField(null=True)

    class Meta:
        table_name = "device_table"

