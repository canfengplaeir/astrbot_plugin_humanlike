from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


def is_mentioned(event: AstrMessageEvent) -> bool:
    """跨平台判断机器人是否被 @ 提及（含 @全体成员）。

    优先使用 AstrBot 事件自带的 is_at_or_wake_command（QQ/Telegram/微信等
    平台适配器均已实现），再回退到手动扫描消息链中的 At 组件（兼容旧版）。
    """
    if getattr(event, "is_at_or_wake_command", False):
        return True

    bot_id = str(getattr(event.message_obj, "self_id", "") or "")
    for comp in getattr(event.message_obj, "message", None) or []:
        cname = type(comp).__name__
        if cname == "At":
            cid = str(getattr(comp, "qq", "") or "")
            if cid == bot_id or cid == "all":
                return True
        elif cname == "AtAll":
            return True
    return False


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

        注意：AI 生成的关键词只有在 use_ai_keywords 开启时才参与匹配，
        否则「AI关键词」开关关闭后旧关键词仍生效，与用户预期不符。
        """
        merged = list(self._interest_keywords)
        if self._reply_cfg.get("use_ai_keywords", False):
            for kw in self._ai_keywords:
                if kw not in merged:
                    merged.append(kw)
        return merged

    @property
    def reply_threshold(self) -> float:
        return float(self._cfg.get("flow_reply_threshold", 20))

    def decay_rate(self) -> float:
        return float(self._cfg.get("flow_decay_rate", 0.15))

    def update(self, state, event, message_text: str, current_time: float,
               persona_name: str = ""):
        """根据时间和消息内容更新心流值。"""
        time_diff = current_time - state.last_update_time
        state.last_update_time = current_time

        decay = time_diff * self.decay_rate()
        state.flow_level = max(0, state.flow_level - decay)

        triggers = []

        if is_mentioned(event):
            boost = float(self._cfg.get("flow_boost_mention", 45))
            state.flow_level = min(100, state.flow_level + boost)
            triggers.append(f"@+{boost:.0f}")
        elif persona_name and persona_name in message_text:
            boost = float(self._cfg.get("flow_boost_mention", 45))
            state.flow_level = min(100, state.flow_level + boost)
            triggers.append(f"名字+{boost:.0f}")

        for kw in self._all_keywords():
            if str(kw).lower() in message_text.lower():
                boost = float(self._cfg.get("flow_boost_keyword", 15))
                state.flow_level = min(100, state.flow_level + boost)
                triggers.append(f"关键词+{boost:.0f}")
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
                f"(间隔={time_diff:.0f}s)"
            )
        if triggers:
            logger.debug(
                f"[群:{event.message_obj.group_id}] 触发: {', '.join(triggers)}"
            )
