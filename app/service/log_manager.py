import os
import logging

from concurrent_log_handler import ConcurrentRotatingFileHandler

from consts.running_consts import LOG_DIR, LOG_MAPPING, DEFAULT_LOG


class LogManager(object):
    def __init__(self, thread_holder="core"):
        self.logger = self.set_log_handler(thread_holder)

    @staticmethod
    def set_log_handler(thread_holder):
        if not os.path.exists(LOG_DIR):
            os.mkdir(LOG_DIR)

        logger = logging.getLogger(thread_holder)
        log_info = LOG_MAPPING.get(thread_holder, DEFAULT_LOG)
        has_rotating_handler = False
        for handler in logger.handlers:
            if isinstance(handler, ConcurrentRotatingFileHandler):
                has_rotating_handler = True
                break
        if not has_rotating_handler:
            handler = ConcurrentRotatingFileHandler(filename=log_info.get("log_name", LOG_DIR + "main.log"),
                                                    maxBytes=log_info.get("max_size", 1 << 20),
                                                    backupCount=log_info.get("backup_count", 10))
            logger.setLevel(level=logging.INFO)
            handler.setLevel(logging.INFO)
            formatter = logging.Formatter(log_info.get("log_format"))
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        return logger

    def info(self, info_str):
        return self.logger.info(info_str)

    def warning(self, warning_str):
        return self.logger.warning(warning_str)

    def error(self, error_str):
        return self.logger.error(error_str)

    def shut_down(self):
        for h in self.logger.handlers[:]:
            h.close()
            self.logger.removeHandler(h)
