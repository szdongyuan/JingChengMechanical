from peewee import AutoField, DoubleField, TextField

from model.base_mode import BaseModel


class TorqueRuleTable(BaseModel):
    id = AutoField()
    match_type = TextField()
    match_key = TextField()
    torque_value = DoubleField()

    class Meta:
        table_name = "torque_rule_table"
        indexes = ((("match_type", "match_key"), True),)
