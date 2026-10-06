import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


def is_mentioned(event: AstrMessageEvent) -> bool:
    """跨平台判断机器人是否被 @ 提及（含 @全体成员）。

    组件优先：消息链中出现 At/AtAll 组件时以组件为准——At 命中自己或
    @全体 → True；At 命中其他成员 → False（即使平台标记
    is_at_or_wake_command 为 True 也覆盖，防止平台误标）；
    消息链中无 At 组件时回退平台标记（兼容 Telegram 等不产生标准 At
    组件的平台）。
    """
    msg = getattr(event.message_obj, "message", None) or []
    bot_id = str(getattr(event.message_obj, "self_id", "") or "")
    has_at = False
    for comp in msg:
        cname = type(comp).__name__
        if cname == "AtAll":
            return True
        if cname == "At":
            has_at = True
            cid = str(getattr(comp, "qq", "") or "")
            if cid == bot_id or cid == "all":
                return True
    if has_at:
        # 有 At 组件但没 @ 自己（@了别人）→ 不算提及
        return False
    return getattr(event, "is_at_or_wake_command", False)


def is_direct_mention(event: AstrMessageEvent) -> bool:
    """是否被直接点名（@ 机器人本人），排除 @全体成员。

    用于「必定回复」语义：@全体是群广播，不算直接点名。
    组件扫描优先：消息链中出现 At/AtAll 组件时以组件为准（At 命中自己 → True，
    否则即使平台标记 is_at_or_wake_command 为 True——@全体/唤醒词场景——也返回 False）；
    消息链中无 At 组件时回退平台标记（部分平台适配器的 @ 不产生标准 At 组件）。
    """
    msg = getattr(event.message_obj, "message", None) or []
    bot_id = str(getattr(event.message_obj, "self_id", "") or "")
    has_at = False
    for comp in msg:
        cname = type(comp).__name__
        if cname == "AtAll":
            has_at = True
        elif cname == "At":
            has_at = True
            if str(getattr(comp, "qq", "") or "") == bot_id:
                return True
    if has_at:
        return False
    return getattr(event, "is_at_or_wake_command", False)


def is_name_mention(event: AstrMessageEvent, persona_name: str = "",
                    aliases: list[str] | None = None) -> bool:
    """Detect QQ official's text-form mention when no At component is emitted.

    QQ official can render ``@Display Name`` in message text while omitting an
    At component. Only an exact name at the beginning of the message counts,
    avoiding false positives for ordinary text that merely mentions the bot.
    """
    text = str(getattr(event, "message_str", "") or "").strip()
    if not text.startswith("@"):
        return False
    if isinstance(aliases, str):
        aliases = aliases.splitlines()
    names = [str(x).strip() for x in (aliases or []) if str(x).strip()]
    # An explicit alias list is authoritative. Fall back to persona name only
    # when the user has not configured platform display names.
    if not names and persona_name and persona_name.strip():
        names.append(persona_name.strip())
    return any(
        re.match(rf"^@\s*{re.escape(name)}(?=\s|$|[，。！？,.!?：:])",
                 text, flags=re.IGNORECASE)
        for name in names
    )


class FlowEngine:
    """心流引擎：追踪并更新机器人在群聊中的参与意愿(0-100)。"""

    def __init__(self, config):
        self._cfg = config.get("flow_engine", {})
        self._reply_cfg = config.get("reply_engine", {})
        self._interest_keywords: list = config.get("interest_keywords", []) or []
        self._ai_keywords: list = []

    def set_ai_keywords(self, keywords: list[str]):
        seen = set()
        cleaned = []
        for kw in keywords or []:
            k = str(kw).strip()
            if k and k.lower() not in seen:
                seen.add(k.lower())
                cleaned.append(k)
        self._ai_keywords = cleaned

    @property
    def has_ai_keywords(self) -> bool:
        return len(self._ai_keywords) > 0

    def _all_keywords(self) -> list:
        """当前生效的全部关键词。

        手动话题始终可用于匹配预览；AI 生成话题只有在 use_ai_keywords 开启时参与。
        """
        merged = []
        seen = set()
        for kw in self._interest_keywords:
            text = str(kw).strip()
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                merged.append(text)
        if self._reply_cfg.get("use_ai_keywords", False):
            for kw in self._ai_keywords:
                text = str(kw).strip()
                key = text.casefold()
                if text and key not in seen:
                    seen.add(key)
                    merged.append(text)
        return merged

    @property
    def reply_threshold(self) -> float:
        return float(self._cfg.get("flow_reply_threshold", 20))

    def decay_rate(self) -> float:
        return float(self._cfg.get("flow_decay_rate", 0.15))

    def decay(self, state, current_time: float) -> float:
        """Advance natural flow decay even when no new message arrives."""
        previous = state.last_update_time or current_time
        elapsed = max(0.0, current_time - previous)
        state.last_update_time = current_time
        amount = elapsed * self.decay_rate()
        state.flow_level = round(max(0.0, min(100.0, state.flow_level - amount)), 1)
        return amount

    def update(self, state, event, message_text: str, current_time: float,
               persona_name: str = ""):
        """根据时间和消息内容更新心流值。"""
        decay = self.decay(state, current_time)

        triggers = []

        if is_mentioned(event) or is_name_mention(
            event, persona_name, self._reply_cfg.get("text_mention_names", [])
        ):
            boost = float(self._cfg.get("flow_boost_mention", 45))
            state.flow_level = min(100, state.flow_level + boost)
            triggers.append(f"@+{boost:.0f}")
        elif persona_name and persona_name in message_text:
            boost = float(self._cfg.get("flow_boost_mention", 45))
            state.flow_level = min(100, state.flow_level + boost)
            triggers.append(f"名字+{boost:.0f}")

        # 话题偏好默认只作为 AI 判断的上下文提示，不直接抬高心流。
        # 需要兼容旧版启发式行为时，可显式打开 keyword_boost_enabled。
        if self._cfg.get("keyword_boost_enabled", False):
            for kw in self._all_keywords():
                if str(kw).casefold() in message_text.casefold():
                    boost = float(self._cfg.get("flow_boost_keyword", 15))
                    state.flow_level = min(100, state.flow_level + boost)
                    triggers.append(f"话题偏好+{boost:.0f}")
                    break

        if "?" in message_text or "？" in message_text:
            boost = float(self._cfg.get("flow_boost_question", 25))
            state.flow_level = min(100, state.flow_level + boost)
            triggers.append(f"问号+{boost:.0f}")

        # 活跃度加成：统计“群消息”而非机器人自己的回复时间戳
        # （旧逻辑统计 reply_timestamps，由于防抖的存在几乎永远不会触发）
        recent_60s = sum(1 for t in state.msg_timestamps
                         if current_time - t < 60)
        if recent_60s >= 3:
            boost = float(self._cfg.get("flow_boost_activity", 3))
            state.flow_level = min(100, state.flow_level + boost)
            triggers.append(f"活跃+{boost:.0f}")

        state.flow_level = round(max(0, min(100, state.flow_level)), 1)

        if decay > 0.5:
            logger.debug(
                f"[群:{event.message_obj.group_id}] 心流衰减 {decay:.1f} "
                f"(按时间推进)"
            )
        if triggers:
            logger.debug(
                f"[群:{event.message_obj.group_id}] 触发: {', '.join(triggers)}"
            )
