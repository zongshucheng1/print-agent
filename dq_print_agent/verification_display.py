"""User-facing queue observations; never infer physical output from a spooler."""
from datetime import datetime


STATE_NAMES = {
    "QUEUED": "队列等待中", "SPOOLING": "正在生成打印数据", "PRINTING": "正在打印",
    "PAUSED": "队列已暂停", "ATTENTION_REQUIRED": "打印机需要处理",
    "SENT_TO_PRINTER": "已发送到打印机", "SPOOLER_REPORTED_PRINTED": "驱动报告完成",
    "DELETED": "任务已删除，出纸结果不明", "NOT_FOUND": "队列无记录，出纸结果不明",
    "IDENTITY_MISMATCH": "队列编号已复用，结果不明", "QUERY_FAILED": "状态读取失败，稍后重查",
    "TIMED_OUT": "等待超时，继续检查", "NO_SPOOL_ID": "无队列编号，无法自动确认",
}


def verification_text(observation) -> str:
    if not isinstance(observation, dict) or not observation.get("state"):
        return "等待客户端自动检查"
    state = observation["state"]
    if state == "ATTENTION_REQUIRED":
        flags = observation.get("statusFlags", 0)
        if type(flags) is int:
            reasons = [label for flag, label in ((0x40, "缺纸"), (0x20, "离线"),
                (0x200, "驱动阻塞"), (0x400, "设备需要处理"), (0x2, "打印错误")) if flags & flag]
            if reasons:
                return " / ".join(reasons)
    return STATE_NAMES.get(state, "状态暂不可识别")


def observation_time(observation) -> str:
    if not isinstance(observation, dict):
        return "—"
    value = observation.get("observedAtMs")
    if type(value) not in (int, float) or value <= 0:
        return "—"
    try:
        return datetime.fromtimestamp(value / 1000).strftime("%m-%d %H:%M:%S")
    except (ValueError, OverflowError, OSError):
        return "—"
