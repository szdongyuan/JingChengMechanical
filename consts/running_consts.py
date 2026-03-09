import os
import sys

DEFAULT_DIR = os.path.split(os.path.realpath(__file__))[0].replace("\\", "/") + "/../"
# basic consts
KB = 1 << 10
MB = 1 << 20
GB = 1 << 30
# log consts

def get_base_dir():
    if getattr(sys, 'frozen', False):
        # exe 运行环境, 取exe当前目录
        base_dir = os.path.dirname(sys.executable)
    else:
        # 源码运行环境，取当前文件的上一级目录
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    return base_dir

base_dir = get_base_dir()

LOG_DIR = os.path.join(base_dir, "log")

DEFAULT_LOG_FORMATTER = '[%(asctime)s][%(name)s] - [%(levelname)s] - [%(message)s] [%(filename)s:%(lineno)d]'

DEFAULT_LOG = {
    "log_name": os.path.join(LOG_DIR, "main.log"),
    "max_size": 2 * MB,
    "backup_count": 9,
    "log_format": DEFAULT_LOG_FORMATTER,
}
AI_LOG = {
    "log_name": os.path.join(LOG_DIR, "ai.log"),
    "max_size": 2 * MB,
    "backup_count": 9,
    "log_format": DEFAULT_LOG_FORMATTER,
}
DEBUG_LOG = {
    "log_name": os.path.join(LOG_DIR, "debug.log"),
    "max_size": 1 * MB,
    "backup_count": 0,
    "log_format": DEFAULT_LOG_FORMATTER,
}

TEST_LOG = {
    "log_name": os.path.join(LOG_DIR, "test.log"),
    "max_size": 100 * KB,
    "backup_count": 0,
    "log_format": DEFAULT_LOG_FORMATTER,
}

LOG_MAPPING = {
    "core": DEFAULT_LOG,
    "train": AI_LOG,
    "evaluate": AI_LOG,
    "predict": AI_LOG,
    "debug": DEBUG_LOG,
    "test": TEST_LOG,
    "db_core": DEFAULT_LOG,
    "soundcard_core": DEFAULT_LOG,
}
