from peewee import AutoField, IntegerField, TextField

from model.base_mode import BaseModel


class PlcTable(BaseModel):
    id = AutoField()
    plc_name = TextField(null=True)
    plc_ip = TextField(null=True)
    plc_rack = IntegerField(null=True)
    plc_slot = IntegerField(null=True)
    plc_db_number = IntegerField(null=True)
    plc_byte_index = IntegerField(null=True)
    plc_bit_index = IntegerField(null=True)
    plc_poll_interval_ms = IntegerField(null=True)
    plc_reconnect_interval_ms = IntegerField(null=True)
    plc_state = IntegerField(null=True) # 0 关闭, 1打开

    class Meta:
        table_name = "plc_table"
