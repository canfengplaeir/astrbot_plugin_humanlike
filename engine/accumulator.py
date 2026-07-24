import asyncio
import random

from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger

from .state import GroupState


class AccumulationManager:
    """累积管理器：消息爆发时缓冲，沉默后批量处理。"""

    def __init__(self, config):
        self._cfg = config.get("accumulation", {})
        self._reply_cfg = config.get("reply_engine", {})

    @property
    def enabled(self) -> bool:
        return self._cfg.get("enabled", True)

    def silence_threshold(self) -> float:
        return float(self._cfg.get("silence_threshold", 3))

    def is_immediate_trigger(self, event: AstrMessageEvent,
                             flow_level: float, persona_name: str) -> bool:
        """判定是否跳过累积，立即处理。"""
        if not self.enabled:
            return True

        bot_id = str(event.message_obj.self_id)
        for comp in event.message_obj.message:
            if type(comp).__name__ == "At" and str(getattr(comp, "qq", "")) == bot_id:
                logger.debug("累积: @提及触发立即回复")
                return True

        names = [n for n in [persona_name, self._reply_cfg.get("bot_name", "")] if n]
        for name in names:
            if name and name in (event.message_str or ""):
                logger.debug(f"累积: 名字'{name}'触发立即回复")
                return True

        threshold = int(self._cfg.get("immediate_flow_threshold", 55))
        if flow_level >= threshold:
            logger.debug(f"累积: 心流{flow_level:.0f}>={threshold}触发立即回复")
            return True

        return False

    def add_to_buffer(self, state: GroupState, event: AstrMessageEvent,
                      message_text: str, sender_name: str):
        state.pending_messages.append({
            "sender": sender_name or "未知",
            "text": message_text,
            "event": event,
        })

    def cancel_timer(self, state: GroupState):
        if state.silence_timer is not None:
            timer = state.silence_timer
            if hasattr(timer, "cancel") and timer.cancel():
                logger.debug("累积: 已取消等待计时器")
            state.silence_timer = None

    async def start_timer(self, group_id: str, state: GroupState,
                          callback):
        """启动计时器，到期后调用 callback(group_id)。"""
        delay = self.silence_threshold()
        delay *= 0.7 + random.random() * 0.6  # ±30% 随机抖动

        async def _on_timer():
            await asyncio.sleep(delay)
            logger.debug(
                f"[群:{group_id}] 累积: {delay}s无新消息，"
                f"触发批量处理 (共{len(state.pending_messages)}条)"
            )
            await callback(group_id)

        state.silence_timer = asyncio.ensure_future(_on_timer())

    def should_force_process(self, state: GroupState) -> bool:
        max_buf = int(self._cfg.get("max_buffer_size", 20))
        return len(state.pending_messages) >= max_buf
