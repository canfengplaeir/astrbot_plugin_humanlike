from astrbot.api import logger


class FlowEngine:
    """心流引擎：追踪并更新机器人在群聊中的参与意愿(0-100)。"""

    def __init__(self, config):
        self._cfg = config.get("flow_engine", {})
        self._reply_cfg = config.get("reply_engine", {})
        self._interest_keywords: list = config.get("interest_keywords", []) or []
        self._ai_keywords: list = []

    def set_ai_keywords(self, keywords: list[str]):
        self._ai_keywords = [kw for kw in keywords if kw]

    @property
    def has_ai_keywords(self) -> bool:
        return len(self._ai_keywords) > 0

    def _all_keywords(self) -> list:
        merged = list(self._interest_keywords)
        for kw in self._ai_keywords:
            if kw not in merged:
                merged.append(kw)
        return merged

    @property
    def reply_threshold(self) -> float:
        return float(self._cfg.get("flow_reply_threshold", 45))

    def decay_rate(self) -> float:
        return float(self._cfg.get("flow_decay_rate", 0.4))

    def update(self, state, event, message_text: str, current_time: float,
               persona_name: str = ""):
        """根据时间和消息内容更新心流值。"""
        time_diff = current_time - state.last_update_time
        state.last_update_time = current_time

        decay = time_diff * self.decay_rate()
        state.flow_level = max(0, state.flow_level - decay)

        bot_id = str(event.message_obj.self_id)
        triggers = []

        is_mentioned = False
        for comp in event.message_obj.message:
            if type(comp).__name__ == "At" and str(getattr(comp, "qq", "")) == bot_id:
                is_mentioned = True
                break

        if is_mentioned:
            boost = float(self._cfg.get("flow_boost_mention", 35))
            state.flow_level = min(100, state.flow_level + boost)
            triggers.append(f"@+{boost:.0f}")
        else:
            names = [n for n in [persona_name,
                    self._reply_cfg.get("bot_name", "")] if n]
            for name in names:
                if name in message_text:
                    boost = float(self._cfg.get("flow_boost_mention", 35))
                    state.flow_level = min(100, state.flow_level + boost)
                    triggers.append(f"名字+{boost:.0f}")
                    break

        for kw in self._all_keywords():
            if str(kw).lower() in message_text.lower():
                boost = float(self._cfg.get("flow_boost_keyword", 15))
                state.flow_level = min(100, state.flow_level + boost)
                triggers.append(f"关键词+{boost:.0f}")
                break

        if "?" in message_text or "？" in message_text:
            boost = float(self._cfg.get("flow_boost_question", 10))
            state.flow_level = min(100, state.flow_level + boost)
            triggers.append(f"问号+{boost:.0f}")

        recent_60s = sum(1 for t in state.reply_timestamps if current_time - t < 60)
        if recent_60s >= 3:
            boost = float(self._cfg.get("flow_boost_activity", 3))
            state.flow_level = min(100, state.flow_level + boost)
            triggers.append(f"活跃+{boost:.0f}")

        state.flow_level = round(max(0, min(100, state.flow_level)), 1)

        if decay > 0.5:
            logger.debug(
                f"[群:{event.message_obj.group_id}] 心流衰减 {decay:.1f} "
                f"(间隔={time_diff:.0f}s)"
            )
        if triggers:
            logger.debug(
                f"[群:{event.message_obj.group_id}] 触发: {', '.join(triggers)}"
            )
