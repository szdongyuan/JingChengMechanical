from peewee import TextField

from model.base_mode import BaseModel


class PlcProcessParamOffsetTable(BaseModel):
    process_ip = TextField(null=True)
    process_db_number = TextField(null=True)
    process_reconnect_interval_ms = TextField(null=True)
    load_cw_scan_offset = TextField(null=True)
    load_cw_load_offset = TextField(null=True)
    load_pause_offset = TextField(null=True)
    load_ccw_scan_offset = TextField(null=True)
    load_ccw_load_offset = TextField(null=True)
    load_to_noload_waiting_time_offset = TextField(null=True)
    switch_sound_cw_1_offset = TextField(null=True)
    switch_sound_ccw_1_offset = TextField(null=True)
    switch_sound_cw_2_offset = TextField(null=True)
    switch_sound_ccw_2_offset = TextField(null=True)
    noload_cw_offset = TextField(null=True)
    noload_pause_offset = TextField(null=True)
    noload_ccw_offset = TextField(null=True)

    class Meta:
        table_name = "plc_process_param_offset_table"
        primary_key = False
