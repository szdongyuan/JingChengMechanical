from __future__ import annotations

from model.base_mode import db
from model.test_config_table import TestConfigTable
from model.channel_config_table import ChannelConfigTable


class DbConnectionService:
    """
    数据库连接服务（长连接）。

    目标：
    - 程序启动时 connect 一次（或首次使用时自动 connect）
    - 程序退出时 close 一次
    - 业务代码/CRUD 不需要每次手动 connect/close
    """

    def connect(self):
        db.connect(reuse_if_open=True)
        db.create_tables([TestConfigTable, ChannelConfigTable], safe=True)

    def close(self):
        if not db.is_closed():
            db.close()

    def is_connected(self) -> bool:
        return not db.is_closed()

