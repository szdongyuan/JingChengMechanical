from peewee import AutoField, IntegerField, TextField

from model.base_mode import BaseModel


class PlcTable(BaseModel):
    id = AutoField()
    plc_name = TextField(null=True)
    plc_ip = TextField(null=True)
    plc_rack = IntegerField(null=True)
    plc_slot = IntegerField(null=True)
    plc_db_number = IntegerField(null=True)
    plc_poll_interval_ms = IntegerField(null=True)       # 轮询时间
    plc_reconnect_interval_ms = IntegerField(null=True)  # 断线重连时间
    # plc_connect_state = IntegerField(null=True) # 0 断开 1已连接, 2连接中  先不用
    plc_open_state = IntegerField(null=True)  # 0 开机不连接, 1开机自动连接

    class Meta:
        table_name = "plc_table"
