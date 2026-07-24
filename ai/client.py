import time

from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger


class AIClient:
    """AI 调用客户端：封装 LLM 判断与回复生成。"""

    def __init__(self, context, config):
        self._ctx = context
        self._cfg = config
        self._re_cfg = config.get("reply_engine", {})

    # ── provider ─────────────────────────────────────────────

    async def _provider_id(self, event: AstrMessageEvent, for_judge: bool = False):
        if for_judge:
            judge_pid = self._re_cfg.get("judge_provider_id", "")
            if judge_pid:
                return judge_pid
        return await self._ctx.get_current_chat_provider_id(
            umo=event.unified_msg_origin
        )

    # ── prompt helpers ───────────────────────────────────────

    def _persona_block(self, system_prompt: str, name: str, short: bool = False) -> str:
        if not self._re_cfg.get("inherit_persona", True) or not system_prompt:
            return f"你的名字是 {name}。\n\n" if name else ""
        if short:
            return (
                f"【你的身份】\n名字：{name or '（见设定）'}\n"
                f"性格：{system_prompt[:300]}\n\n"
            )
        return f"【核心人格设定（必须严格遵守）】\n{system_prompt}\n\n"

    def _style_line(self, flow_level: float,
                    persona_system_prompt: str) -> str:
        style = self._cfg.get("reply_style", "随性自然")
        if flow_level >= 70:
            hint = "（兴致较高，可多说一点）"
        elif flow_level < 50:
            hint = "（兴致一般，保持简短）"
        else:
            hint = ""
        if self._re_cfg.get("inherit_persona", True) and persona_system_prompt:
            return f"语气微调：{style} {hint}\n"
        return f"回复风格：{style} {hint}\n"

    def _judge_instructions(self) -> str:
        return self._re_cfg.get("ai_judge_prompt",
            self._cfg.get("ai_judge_prompt",
                "你是一个群聊成员。根据上下文判断是否发言。\n"
                "原则：@提及必须回复 / 能回答的问题可以发言 / "
                "感兴趣的话题可以参与 / 灌水不需要回复\n"
                "请只回复「发言」或「沉默」。"
            ))

    def _reply_instructions(self) -> str:
        return self._re_cfg.get("ai_reply_prompt",
            self._cfg.get("ai_reply_prompt",
                "请自然地回复。简洁1-3句，像真人一样，不要机械感。"
            ))

    # ── 单条消息 ─────────────────────────────────────────────

    async def judge(self, event: AstrMessageEvent, flow_level: float,
                    context: list[dict],
                    persona_system_prompt: str = "",
                    persona_name: str = "") -> bool:
        if not self._re_cfg.get("enable_ai_judge", True):
            return flow_level >= float(
                self._cfg.get("flow_engine", {}).get("flow_reply_threshold", 45)
            ) + 15

        try:
            ctx = "\n".join(f"[{m['sender']}]: {m['text']}" for m in context[-6:])
            prompt = (
                f"{self._persona_block(persona_system_prompt, persona_name, short=True)}"
                f"{self._judge_instructions()}\n\n"
                f"心流值：{flow_level:.0f}/100\n\n"
                f"最近群聊：\n{ctx or '（暂无）'}\n\n"
                f"最新消息 — {event.get_sender_name() or '某人'}：{event.message_str}\n\n"
                f"请只回复「发言」或「沉默」："
            )
            pid = await self._provider_id(event, for_judge=True)
            if not pid:
                return flow_level >= 80

            t0 = time.time()
            resp = await self._ctx.llm_generate(chat_provider_id=pid, prompt=prompt)
            result = resp.completion_text.strip()
            elapsed = (time.time() - t0) * 1000

            if "发言" in result and "沉默" not in result and len(result) <= 5:
                logger.debug(f"[AI判断] 心流={flow_level:.0f} → 发言 ({elapsed:.0f}ms)")
                return True
            logger.debug(f"[AI判断] 心流={flow_level:.0f} → 沉默 ({elapsed:.0f}ms)")
            return False
        except Exception as e:
            logger.error(f"AI判断失败: {e}")
            return flow_level >= 75

    async def reply(self, event: AstrMessageEvent, flow_level: float,
                    context: list[dict],
                    persona_system_prompt: str = "",
                    persona_name: str = "") -> str:
        try:
            ctx = "\n".join(f"[{m['sender']}]: {m['text']}" for m in context[-8:])
            prompt = (
                f"{self._persona_block(persona_system_prompt, persona_name)}"
                f"【行为指令】\n{self._reply_instructions()}\n\n"
                f"{self._style_line(flow_level, persona_system_prompt)}"
                f"心流值：{flow_level:.0f}/100\n\n"
                f"最近群聊：\n{ctx}\n\n"
                f"{event.get_sender_name() or '某人'} 说：「{event.message_str}」\n\n"
                f"回复："
            )
            pid = await self._provider_id(event)
            if not pid:
                return ""

            t0 = time.time()
            resp = await self._ctx.llm_generate(chat_provider_id=pid, prompt=prompt)
            elapsed = (time.time() - t0) * 1000
            reply_text = resp.completion_text.strip()
            reply_text = reply_text.replace("回复：", "").replace("回复:", "").strip()
            logger.debug(f"[AI回复] 生成完毕 ({elapsed:.0f}ms, {len(reply_text)}字)")
            return reply_text or ""
        except Exception as e:
            logger.error(f"AI回复生成失败: {e}")
            return ""

    # ── 批量消息 ─────────────────────────────────────────────

    async def judge_batch(self, event: AstrMessageEvent, flow_level: float,
                          context: list[dict],
                          persona_system_prompt: str = "",
                          persona_name: str = "") -> bool:
        if not self._re_cfg.get("enable_ai_judge", True):
            return flow_level >= float(
                self._cfg.get("flow_engine", {}).get("flow_reply_threshold", 45)
            ) + 15

        try:
            ctx = "\n".join(f"[{m['sender']}]: {m['text']}" for m in context[-10:])
            prompt = (
                f"{self._persona_block(persona_system_prompt, persona_name, short=True)}"
                f"{self._judge_instructions()}\n\n"
                f"心流值：{flow_level:.0f}/100\n"
                f"【注意】以下是一段时间内累积的消息，请综合判断是否该参与。\n\n"
                f"群聊记录：\n{ctx or '（暂无）'}\n\n"
                f"请只回复「发言」或「沉默」："
            )
            pid = await self._provider_id(event, for_judge=True)
            if not pid:
                return flow_level >= 80

            t0 = time.time()
            resp = await self._ctx.llm_generate(chat_provider_id=pid, prompt=prompt)
            result = resp.completion_text.strip()
            elapsed = (time.time() - t0) * 1000

            if "发言" in result and "沉默" not in result and len(result) <= 5:
                logger.debug(f"[AI批量判断] 心流={flow_level:.0f} → 发言 ({elapsed:.0f}ms)")
                return True
            logger.debug(f"[AI批量判断] 心流={flow_level:.0f} → 沉默 ({elapsed:.0f}ms)")
            return False
        except Exception as e:
            logger.error(f"AI批量判断失败: {e}")
            return flow_level >= 75

    async def reply_batch(self, event: AstrMessageEvent, flow_level: float,
                          context: list[dict],
                          persona_system_prompt: str = "",
                          persona_name: str = "") -> str:
        try:
            ctx = "\n".join(f"[{m['sender']}]: {m['text']}" for m in context[-10:])
            prompt = (
                f"{self._persona_block(persona_system_prompt, persona_name)}"
                f"【行为指令】\n{self._reply_instructions()}\n\n"
                f"{self._style_line(flow_level, persona_system_prompt)}"
                f"心流值：{flow_level:.0f}/100\n"
                f"【注意】以下是最近一段时间的群聊记录，请综合上下文后自然地参与讨论。\n\n"
                f"群聊记录：\n{ctx}\n\n"
                f"回复："
            )
            pid = await self._provider_id(event)
            if not pid:
                return ""

            t0 = time.time()
            resp = await self._ctx.llm_generate(chat_provider_id=pid, prompt=prompt)
            elapsed = (time.time() - t0) * 1000
            reply_text = resp.completion_text.strip()
            reply_text = reply_text.replace("回复：", "").replace("回复:", "").strip()
            logger.debug(f"[AI批量回复] 生成完毕 ({elapsed:.0f}ms, {len(reply_text)}字)")
            return reply_text or ""
        except Exception as e:
            logger.error(f"AI批量回复生成失败: {e}")
            return ""
