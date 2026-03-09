from peewee import AutoField, TextField

from consts.time_consts import now_str
from model.base_mode import BaseModel


class UsersTable(BaseModel):
    """
    对应表：users_table

    触发器 ensure_one_admin_user（只允许一个 Admin）在数据库里生效，
    model 不需要实现触发器逻辑。
    """

    user_id = AutoField()
    user_name = TextField(unique=True)
    password = TextField()
    access_level = TextField()
    user_created_time = TextField(null=True, default=now_str)
    user_updated_time = TextField(null=True, default=now_str)

    class Meta:
        table_name = "users_table"

