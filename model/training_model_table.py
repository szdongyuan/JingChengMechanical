from peewee import FloatField, IntegerField, TextField

from model.base_mode import BaseModel


class TrainingModelTable(BaseModel):
    """
    对应表：training_model_table

    SQL 里有约束 (accuracy >= 0 and accuracy <= 1)；这里先按字段建模，约束由 DB 侧保证。
    """

    model_id = TextField(primary_key=True)
    model_name = TextField(unique=True)
    model_path = TextField(unique=True)
    config_path = TextField()
    input_dim = TextField()
    output_dim = IntegerField()
    accuracy = FloatField(null=True)
    update_date = TextField()
    model_description = TextField(null=True)

    class Meta:
        table_name = "training_model_table"

