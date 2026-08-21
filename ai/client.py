import asyncio
import time

from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger

from ..engine.flow import is_direct_mention


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

    async def _provider_id_by_umo(self, umo: str):
        return await self._ctx.get_current_chat_provider_id(umo=umo)

    # ── 超时与调用封装 ────────────────────────────────────────

    def _timeout(self, kind: str, default: float) -> float:
        try:
            return float(self._cfg.get("ai_timeout", {}).get(f"{kind}_seconds",
                                                             default))
        except (TypeError, ValueError):
            return default

    async def _llm(self, pid: str, prompt: str, timeout: float) -> str:
        """带超时地调用 LLM，返回文本；失败返回空串。"""
        resp = await asyncio.wait_for(
            self._ctx.llm_generate(chat_provider_id=pid, prompt=prompt),
            timeout=timeout,
        )
        return (resp.completion_text or "").strip()

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

    def _flow_threshold(self) -> float:
        return float(self._cfg.get("flow_engine", {}).get(
            "flow_reply_threshold", 20))

    @staticmethod
    def _mention_note(event: AstrMessageEvent) -> str:
        """被直接点名时的提示语，用于让 LLM 明确感知「直接点名」这一事实。

        仅对点名机器人本人生效（@全体不算），与「必定回复」语义一致。
        """
        if is_direct_mention(event):
            return "【注意】你被 @ 了，原则上必须回复。\n\n"
        return ""

    @staticmethod
    def _latest_line(event: AstrMessageEvent) -> str:
        """构造「最新消息」描述行；单纯 @（无文字）时也能表达清楚。"""
        sender = event.get_sender_name() or "某人"
        text = (event.message_str or "").strip()
        if is_direct_mention(event):
            return f"{sender} @了你" + (f"，说：「{text}」" if text else "")
        return f"{sender} 说：「{text}」"

    # ── 判断结果解析 ─────────────────────────────────────────

    @staticmethod
    def parse_judge(result: str) -> bool | None:
        """解析 LLM 的「发言/沉默」判断。

        返回 True=发言、False=沉默、None=无法解析（由调用方回退启发式规则）。
        兼容模型输出带标点、引号、多余解释等常见情况。
        """
        text = (result or "").strip()
        if not text:
            return None
        text = text.strip('"\'“”‘’`*#~.。，, ')
        lower = text.lower()

        def _speak(t: str) -> bool:
            return ("发言" in t or lower in ("speak", "yes", "y")
                    or lower.startswith("speak"))

        def _silent(t: str) -> bool:
            return ("沉默" in t or "不说话" in t or "不发言" in t
                    or "不参与" in t or lower in ("silent", "no", "n")
                    or lower.startswith("silent"))

        # 含转折词时只看转折后的部分，如「可以发言，但我选择沉默」→ 沉默
        if "但" in text:
            tail = text[text.rfind("但"):]
            if _silent(tail) and not _speak(tail):
                return False
            if _speak(tail) and not _silent(tail):
                return True

        has_speak, has_silence = _speak(text), _silent(text)
        if has_speak and not has_silence:
            return True
        if has_silence and not has_speak:
            return False
        if has_speak and has_silence:
            # 两者都出现且无转折词，以先出现者为准
            i_speak = text.find("发言")
            i_silence = text.find("沉默")
            if i_speak >= 0 and i_silence >= 0:
                return i_speak < i_silence
            return False
        return None

    @staticmethod
    def clean_reply(text: str) -> str:
        """清洗模型生成的回复文本。"""
        t = (text or "").strip()
        if not t:
            return ""
        for pre in ("回复：", "回复:", "回答：", "回答:"):
            if t.startswith(pre):
                t = t[len(pre):].strip()
                break
        if len(t) >= 2 and t[0] in "\"'“”‘’" and t[-1] in "\"'“”‘’":
            t = t[1:-1].strip()
        # 去掉 markdown 加粗/斜体标记
        t = t.replace("**", "").replace("__", "")
        # 去掉防御性判断词的泄漏（仅当整条回复就是「沉默/发言」时）
        t = t.strip()
        if not t or t in ("沉默", "发言", "沉默。", "沉默.", "发言。", "发言."):
            return ""
        # 合并多余空行
        lines = [ln for ln in t.splitlines() if ln.strip()]
        if not lines:
            return ""
        return "\n".join(lines).strip()

    # ── 单条消息 ─────────────────────────────────────────────

    async def judge(self, event: AstrMessageEvent, flow_level: float,
                    context: list[dict],
                    persona_system_prompt: str = "",
                    persona_name: str = "") -> bool:
        if not self._re_cfg.get("enable_ai_judge", True):
            return flow_level >= self._flow_threshold() + 15

        try:
            ctx = "\n".join(f"[{m['sender']}]: {m['text']}" for m in context[-6:])
            prompt = (
                f"{self._persona_block(persona_system_prompt, persona_name, short=True)}"
                f"{self._judge_instructions()}\n\n"
                f"心流值：{flow_level:.0f}/100\n\n"
                f"{self._mention_note(event)}"
                f"最近群聊：\n{ctx or '（暂无）'}\n\n"
                f"最新消息 — {self._latest_line(event)}\n\n"
                f"请只回复「发言」或「沉默」："
            )
            pid = await self._provider_id(event, for_judge=True)
            if not pid:
                return flow_level >= 80

            t0 = time.time()
            result = await self._llm(pid, prompt,
                                     self._timeout("judge", 20))
            elapsed = (time.time() - t0) * 1000

            decision = self.parse_judge(result)
            if decision is not None:
                logger.debug(
                    f"[AI判断] 心流={flow_level:.0f} → "
                    f"{'发言' if decision else '沉默'} ({elapsed:.0f}ms)")
                return decision
            logger.warning(
                f"[AI判断] 无法解析输出（{result[:30]!r}），按沉默处理 "
                f"({elapsed:.0f}ms)")
            return False
        except asyncio.TimeoutError:
            logger.warning(f"[AI判断] 超时（{self._timeout('judge', 20)}s）→ 沉默")
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
                f"{self._latest_line(event)}\n\n"
                f"回复："
            )
            pid = await self._provider_id(event)
            if not pid:
                return ""

            t0 = time.time()
            raw = await self._llm(pid, prompt,
                                  self._timeout("reply", 60))
            elapsed = (time.time() - t0) * 1000
            reply_text = self.clean_reply(raw)
            logger.debug(f"[AI回复] 生成完毕 ({elapsed:.0f}ms, {len(reply_text)}字)")
            return reply_text
        except asyncio.TimeoutError:
            logger.warning(f"[AI回复] 超时（{self._timeout('reply', 60)}s）")
            return ""
        except Exception as e:
            logger.error(f"AI回复生成失败: {e}")
            return ""

    # ── 批量消息 ─────────────────────────────────────────────

    async def judge_batch(self, event: AstrMessageEvent, flow_level: float,
                          context: list[dict],
                          persona_system_prompt: str = "",
                          persona_name: str = "") -> bool:
        if not self._re_cfg.get("enable_ai_judge", True):
            return flow_level >= self._flow_threshold() + 15

        try:
            ctx = "\n".join(f"[{m['sender']}]: {m['text']}" for m in context[-10:])
            prompt = (
                f"{self._persona_block(persona_system_prompt, persona_name, short=True)}"
                f"{self._judge_instructions()}\n\n"
                f"心流值：{flow_level:.0f}/100\n"
                f"{self._mention_note(event)}"
                f"【注意】以下是一段时间内累积的消息，请综合判断是否该参与。\n\n"
                f"群聊记录：\n{ctx or '（暂无）'}\n\n"
                f"请只回复「发言」或「沉默」："
            )
            pid = await self._provider_id(event, for_judge=True)
            if not pid:
                return flow_level >= 80

            t0 = time.time()
            result = await self._llm(pid, prompt,
                                     self._timeout("judge", 20))
            elapsed = (time.time() - t0) * 1000

            decision = self.parse_judge(result)
            if decision is not None:
                logger.debug(
                    f"[AI批量判断] 心流={flow_level:.0f} → "
                    f"{'发言' if decision else '沉默'} ({elapsed:.0f}ms)")
                return decision
            logger.warning(
                f"[AI批量判断] 无法解析输出（{result[:30]!r}），按沉默处理 "
                f"({elapsed:.0f}ms)")
            return False
        except asyncio.TimeoutError:
            logger.warning(f"[AI批量判断] 超时（{self._timeout('judge', 20)}s）→ 沉默")
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
                f"{self._mention_note(event)}"
                f"【注意】以下是最近一段时间的群聊记录，请综合上下文后自然地参与讨论。\n\n"
                f"群聊记录：\n{ctx}\n\n"
                f"回复："
            )
            pid = await self._provider_id(event)
            if not pid:
                return ""

            t0 = time.time()
            raw = await self._llm(pid, prompt,
                                  self._timeout("reply", 60))
            elapsed = (time.time() - t0) * 1000
            reply_text = self.clean_reply(raw)
            logger.debug(f"[AI批量回复] 生成完毕 ({elapsed:.0f}ms, {len(reply_text)}字)")
            return reply_text
        except asyncio.TimeoutError:
            logger.warning(f"[AI批量回复] 超时（{self._timeout('reply', 60)}s）")
            return ""
        except Exception as e:
            logger.error(f"AI批量回复生成失败: {e}")
            return ""

    # ── 主动发言 ─────────────────────────────────────────────

    async def proactive_topic(self, umo: str, idle_minutes: float,
                              context: list[dict],
                              persona_system_prompt: str = "",
                              persona_name: str = "") -> str:
        """群聊冷场后，让 LLM 决定是否主动起话题。

        返回要主动说的话；空串表示「保持沉默」。
        """
        try:
            ctx = "\n".join(f"[{m['sender']}]: {m['text']}" for m in context[-6:])
            prompt = (
                f"{self._persona_block(persona_system_prompt, persona_name, short=True)}"
                f"你是一个群聊成员。群里已经安静了大约{idle_minutes:.0f}分钟。\n"
                f"你可以选择主动说点什么（起个话题、分享见闻、吐槽一下都行，"
                f"要符合你的人设和最近的聊天内容），也可以选择继续安静。\n\n"
                f"最近的聊天记录：\n{ctx or '（暂无记录）'}\n\n"
                f"如果你不想说话，只回复「沉默」两个字。\n"
                f"如果想说话，直接输出你想说的话（1-2句，不要任何前缀解释）："
            )
            pid = await self._provider_id_by_umo(umo)
            if not pid:
                return ""

            t0 = time.time()
            raw = await self._llm(pid, prompt,
                                  self._timeout("proactive", 30))
            elapsed = (time.time() - t0) * 1000

            text = self.clean_reply(raw)
            if not text or text.startswith("沉默"):
                if text:
                    logger.debug(f"[主动发言] 模型选择沉默（{text[:20]!r}）")
                return ""
            logger.info(f"[主动发言] 生成话题 ({elapsed:.0f}ms): {text[:40]}")
            return text
        except asyncio.TimeoutError:
            logger.warning(f"[主动发言] 超时（{self._timeout('proactive', 30)}s）")
            return ""
        except Exception as e:
            logger.error(f"主动发言生成失败: {e}")
            return ""

    # ── 关键词生成 ────────────────────────────────────────────

    async def generate_keywords(self, event: AstrMessageEvent | None = None,
                                persona_prompt: str = "",
                                persona_name: str = "",
                                count: int = 10,
                                umo: str | None = None) -> list[str]:
        """根据人格设定，由 AI 生成感兴趣的关键词列表。

        event 为空时（如 WebUI 调用）使用 umo 指定的会话提供商；
        两者都为空时使用默认聊天模型。
        """
        prompt = (
            f"根据以下人格设定，生成{count}个该角色可能感兴趣的话题关键词。\n"
            f"关键词应该是简短的词语（1-4个字），每行一个，不要编号。\n\n"
            f"人格名称：{persona_name or '（未设定）'}\n"
            f"人格设定：\n{persona_prompt[:500]}\n\n"
            f"关键词："
        )
        try:
            logger.info(f"正在调用 LLM 生成关键词... persona={persona_name}")
            if event is not None:
                pid = await self._provider_id(event, for_judge=False)
            else:
                pid = await self._provider_id_by_umo(umo)
            logger.info(f"关键词生成使用 provider: {pid}")
            if not pid:
                logger.warning("无可用 provider，关键词生成失败")
                return []
            t0 = time.time()
            text = await self._llm(pid, prompt,
                                   self._timeout("keywords", 60))
            elapsed = (time.time() - t0) * 1000
            logger.info(f"LLM 返回 ({elapsed:.0f}ms): {text[:100]}")
            keywords = [
                line.strip().lstrip("0123456789.、-•· ") for line in text.split("\n")
                if line.strip()
            ]
            keywords = [kw for kw in keywords if len(kw) <= 8]
            keywords = keywords[:count]
            logger.info(f"AI生成关键词 ({len(keywords)}个): {keywords[:10]}")
            return keywords
        except asyncio.TimeoutError:
            logger.error(f"关键词生成超时（{self._timeout('keywords', 60)}s）")
            return []
        except Exception as e:
            logger.error(f"关键词生成失败: {e}")
            return []
