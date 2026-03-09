from datetime import datetime, timezone, timedelta

# 你 SQLite 里用的是 DATETIME('now', '+8 hours') 这种文本时间
_TZ8 = timezone(timedelta(hours=8))
def now_str():
    return datetime.now(_TZ8).strftime("%Y-%m-%d %H:%M:%S")