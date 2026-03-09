from peewee import TextField

from model.base_mode import BaseModel


class SystemInfoTable(BaseModel):
    """对应表：system_info_table"""

    name = TextField(primary_key=True)
    value = TextField(null=True)

    class Meta:
        table_name = "system_info_table"

