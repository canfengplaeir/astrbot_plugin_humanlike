import random

from astrbot.api import logger


class DebounceChecker:
    """防抖检查器：冷却时间、频率限制、心流阈值、随机沉默。"""

    def __init__(self, config):
        self._dc = config.get("debounce", {})
        self._fc = config.get("flow_engine", {})

    def check(self, state, current_time: float) -> bool:
        cooldown = float(self._dc.get("min_reply_cooldown", 8))
        if self._dc.get("dynamic_cooldown_enabled", True):
            flow_ratio = state.flow_level / 100.0
            min_cd = float(self._dc.get("min_dynamic_cooldown", 10))
            max_cd = float(self._dc.get("max_dynamic_cooldown", 60))
            cooldown = max_cd - flow_ratio * (max_cd - min_cd)

        time_since = current_time - state.last_reply_time
        if time_since < cooldown:
            logger.debug(f"防抖: 冷却中 (已过{time_since:.0f}s, 需{cooldown:.0f}s)")
            return False

        max_msgs = int(self._dc.get("max_replies_per_window", 12))
        window = float(self._dc.get("reply_window_seconds", 300))
        recent = [t for t in state.reply_timestamps if current_time - t < window]
        if len(recent) >= max_msgs:
            logger.debug(f"防抖: 频率限制 ({len(recent)}/{max_msgs})")
            return False

        threshold = float(self._fc.get("flow_reply_threshold", 20))
        if state.flow_level < threshold:
            logger.debug(f"防抖: 心流不足 ({state.flow_level:.1f}<{threshold:.0f})")
            return False

        silence_prob = float(self._fc.get("random_silence_probability", 2.0))
        if random.random() * 100 < silence_prob:
            logger.debug(f"防抖: 随机沉默 ({silence_prob}%)")
            return False

        logger.debug(
            f"防抖: 通过 | 心流={state.flow_level:.0f} "
            f"冷却={cooldown:.0f}s 窗口={len(recent)}/{max_msgs}"
        )
        return True
