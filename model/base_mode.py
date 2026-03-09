from __future__ import annotations

import os

from peewee import Model, SqliteDatabase

from consts.running_consts import base_dir


# 允许通过环境变量覆盖 DB 路径（方便你对接既有数据库文件）
DB_PATH = os.environ.get("APP_DB_PATH", os.path.join(base_dir, "data", "jingchen.db"))

# 你现有数据库已建好，这里只负责连接，不做建表/迁移
db = SqliteDatabase(DB_PATH, pragmas={"foreign_keys": 1})


class BaseModel(Model):
    class Meta:
        database = db

