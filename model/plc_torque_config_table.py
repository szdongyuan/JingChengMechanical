from peewee import IntegerField, SQL, TextField

from model.base_mode import BaseModel


class PlcTorqueConfigTable(BaseModel):
    plc_id = IntegerField(primary_key=True)
    enabled = IntegerField(default=False, constraints=[SQL("DEFAULT 0")])
    match_type = TextField(default="model", constraints=[SQL("DEFAULT 'model'")])
    read_db_number = IntegerField(null=True)
    read_byte_offset = IntegerField(null=True)
    write_db_number = IntegerField(null=True)
    write_byte_offset = IntegerField(null=True)

    class Meta:
        table_name = "plc_torque_config_table"
