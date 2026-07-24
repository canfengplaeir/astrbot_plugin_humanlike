import asyncio
import os
import sys
import time
from typing import Dict

# 确保插件子目录可被导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import AstrBotConfig
from astrbot.api import logger
from astrbot.core.agent.message import (
    AssistantMessageSegment,
    UserMessageSegment,
    TextPart,
)

from engine import GroupState, FlowEngine, DebounceChecker, AccumulationManager
from ai import PersonaBridge, AIClient


class HumanLikePlugin(Star):
    """拟人化群聊助手 — 编排各模块协同工作。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._states: Dict[str, GroupState] = {}
        self._lock = asyncio.Lock()

        self.flow = FlowEngine(config)
        self.debounce = DebounceChecker(config)
        self.accum = AccumulationManager(config)
        self.persona = PersonaBridge(context, config)
        self.ai = AIClient(context, config)
        self._keywords_loaded = False
        self._keywords_generating = False

        asyncio.ensure_future(self._init_keywords())

        logger.info("拟人化群聊助手插件已加载")

    async def _init_keywords(self):
        if not self.config.get("reply_engine", {}).get("use_ai_keywords", False):
            logger.info("AI关键词生成未启用")
            return
        try:
            saved = await self.get_kv_data("ai_keywords", None)
            if saved and isinstance(saved, list) and len(saved) > 0:
                self.flow.set_ai_keywords(saved)
                logger.info(f"已加载 {len(saved)} 个AI生成关键词")
            else:
                logger.info("无已保存的AI关键词，将在首次消息时自动生成")
        except Exception as e:
            logger.debug(f"加载AI关键词失败（首次使用正常）: {e}")
        self._keywords_loaded = True

    async def _gen_and_save_keywords(self, event: AstrMessageEvent) -> list[str]:
        self._keywords_generating = True
        try:
            persona_prompt = await self.persona.system_prompt(event)
            persona_name = await self.persona.name(event)
            if not persona_prompt:
                logger.warning("无人格设定，无法生成关键词")
                return []
            keywords = await self.ai.generate_keywords(event, persona_prompt, persona_name)
            if keywords:
                self.flow.set_ai_keywords(keywords)
                await self.put_kv_data("ai_keywords", keywords)
                logger.info(f"AI生成并保存 {len(keywords)} 个关键词")
            return keywords
        finally:
            self._keywords_generating = False

    # ============================================================
    # 状态管理
    # ============================================================

    async def _get_state(self, group_id: str) -> GroupState:
        async with self._lock:
            if group_id not in self._states:
                init = float(self.config.get("flow_engine", {}).get("initial_flow", 50))
                self._states[group_id] = GroupState(
                    flow_level=init,
                    last_update_time=time.time(),
                )
            return self._states[group_id]

    def _override(self) -> bool:
        return self.config.get("reply_engine", {}).get("override_group_replies", True)

    def _stop_if_override(self, event: AstrMessageEvent):
        if self._override():
            event.stop_event()

    # ============================================================
    # 主入口
    # ============================================================

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        group_id = event.message_obj.group_id
        if not group_id:
            return

        sender_id = str(event.get_sender_id())
        if sender_id == str(event.message_obj.self_id):
            return

        cfg = self.config
        msg_preview = (event.message_str or "")[:50]

        if cfg.get("reply_engine", {}).get("allow_command_pass", True):
            if (event.message_str or "").strip().startswith("/"):
                logger.debug(f"[群:{group_id}] 指令放行: {msg_preview}")
                return

        logger.info(
            f"[群:{group_id}] 收到消息 | "
            f"{event.get_sender_name()}({sender_id}) | {msg_preview}"
        )

        state = await self._get_state(group_id)
        persona_prompt, persona_name = "", ""
        if self.persona.enabled:
            persona_prompt = await self.persona.system_prompt(event)
            persona_name = await self.persona.name(event)

        if self.config.get("reply_engine", {}).get("use_ai_keywords", False):
            if hasattr(self.flow, 'has_ai_keywords') and not self.flow.has_ai_keywords and self._keywords_loaded and not self._keywords_generating:
                asyncio.ensure_future(self._gen_and_save_keywords(event))

        now = time.time()
        msg_text = event.message_str or ""

        async with state.lock:
            old_flow = state.flow_level
            self.flow.update(state, event, msg_text, now, persona_name)
            if state.flow_level != old_flow:
                logger.debug(
                    f"[群:{group_id}] 心流 {old_flow:.1f} → {state.flow_level:.1f}"
                )

            immediate = self.accum.is_immediate_trigger(
                event, state.flow_level, persona_name
            )

            if immediate:
                self.accum.cancel_timer(state)
                for m in state.pending_messages:
                    state.conversation_context.append({
                        "sender": m["sender"], "text": m["text"],
                    })
                if len(state.conversation_context) > 12:
                    state.conversation_context = state.conversation_context[-12:]
                state.pending_messages.clear()

                self._append_context(state, event.get_sender_name() or "未知", msg_text)
                if not self.debounce.check(state, now):
                    logger.info(f"[群:{group_id}] 防抖拦截（立即触发但频率受限）")
                    self._stop_if_override(event)
                    return

                flow_snap = state.flow_level
                ctx_snap = list(state.conversation_context[-8:])

            else:
                if self.accum.enabled:
                    self.accum.add_to_buffer(state, event, msg_text,
                                             event.get_sender_name() or "未知")
                    state.last_msg_time = now
                    self.accum.cancel_timer(state)
                    await self.accum.start_timer(group_id, state,
                                                 self._on_silence_timeout)

                    if self.accum.should_force_process(state):
                        logger.debug(f"[群:{group_id}] 累积缓冲满，立即处理")
                        self.accum.cancel_timer(state)
                        asyncio.ensure_future(self._run_batch_pipeline(group_id))

                    logger.info(
                        f"[群:{group_id}] 累积: 缓冲({len(state.pending_messages)}条, "
                        f"心流={state.flow_level:.0f})"
                    )
                else:
                    self._append_context(state, event.get_sender_name() or "未知",
                                         msg_text)
                    if not self.debounce.check(state, now):
                        logger.info(f"[群:{group_id}] 防抖拦截")
                        self._stop_if_override(event)
                        return
                    flow_snap = state.flow_level
                    ctx_snap = list(state.conversation_context[-8:])
                    immediate = True  # 走立即处理路径

                self._stop_if_override(event)
                if self.accum.enabled:
                    return

        await self._run_immediate(event, state, flow_snap, ctx_snap,
                                  persona_prompt, persona_name)

    # ============================================================
    # 立即处理
    # ============================================================

    async def _run_immediate(self, event, state, flow_snap, ctx_snap,
                             persona_prompt, persona_name):
        logger.info(f"立即处理 | 心流={flow_snap:.0f} → AI判断...")
        if not await self.ai.judge(event, flow_snap, ctx_snap,
                                    persona_prompt, persona_name):
            logger.info("AI判断 → 沉默")
            self._stop_if_override(event)
            return

        logger.info("AI判断 → 发言 → 生成回复...")
        reply = await self.ai.reply(event, flow_snap, ctx_snap,
                                     persona_prompt, persona_name)
        if not reply:
            logger.warning("回复生成失败（空内容）")
            self._stop_if_override(event)
            return

        logger.info(f"回复完毕 ({len(reply)}字) | 预览={reply[:40]}")

        await event.send(event.plain_result(reply))

        async with state.lock:
            self._record_reply(state, now=time.time(),
                               persona_name=persona_name)

            state.conversation_context.append({
                "sender": persona_name or self.config.get("reply_engine", {}).get("bot_name", "bot"),
                "text": reply,
            })
            if len(state.conversation_context) > 12:
                state.conversation_context = state.conversation_context[-12:]

        await self._write_history(event, reply)
        self._stop_if_override(event)

    # ============================================================
    # 累积批处理
    # ============================================================

    async def _on_silence_timeout(self, group_id: str):
        await self._run_batch_pipeline(group_id)

    async def _run_batch_pipeline(self, group_id: str):
        state = await self._get_state(group_id)

        async with state.lock:
            if not state.pending_messages:
                return
            pending = list(state.pending_messages)

        logger.info(
            f"[群:{group_id}] 批处理: {len(pending)}条 "
            f"| {' → '.join(m['text'][:20] for m in pending[-3:])}"
        )

        now = time.time()
        if not self.debounce.check(state, now):
            logger.info(f"[群:{group_id}] 批处理: 防抖拦截，延时重试")
            await self.accum.start_timer(group_id, state, self._on_silence_timeout)
            return

        async with state.lock:
            state.pending_messages.clear()
            state.silence_timer = None
            for m in pending:
                state.conversation_context.append({
                    "sender": m["sender"], "text": m["text"],
                })
            if len(state.conversation_context) > 12:
                state.conversation_context = state.conversation_context[-12:]

        last_event = pending[-1].get("event") if pending else None
        if not last_event:
            return

        persona_prompt, persona_name = "", ""
        if self.persona.enabled:
            persona_prompt = await self.persona.system_prompt(last_event)
            persona_name = await self.persona.name(last_event)

        ctx_list = list(state.conversation_context[-10:])
        flow_snap = state.flow_level

        if not await self.ai.judge_batch(last_event, flow_snap, ctx_list,
                                          persona_prompt, persona_name):
            logger.info(f"[群:{group_id}] 批处理: AI判断→沉默")
            return

        logger.info(f"[群:{group_id}] 批处理: AI判断→发言 → 生成...")
        reply = await self.ai.reply_batch(last_event, flow_snap, ctx_list,
                                           persona_prompt, persona_name)
        if not reply:
            logger.warning(f"[群:{group_id}] 批处理: 回复生成失败")
            return

        logger.info(f"[群:{group_id}] 批处理: 回复完毕 ({len(reply)}字) | 预览={reply[:40]}")

        async with state.lock:
            self._record_reply(state, now=time.time(),
                               persona_name=persona_name)
            state.conversation_context.append({
                "sender": persona_name or self.config.get("reply_engine", {}).get("bot_name", "bot"),
                "text": reply,
            })
            if len(state.conversation_context) > 12:
                state.conversation_context = state.conversation_context[-12:]

        await last_event.send(last_event.plain_result(reply))

        combined_user = " | ".join([m["text"] for m in pending])
        await self._write_history(last_event, reply, user_message=combined_user)

        if self._override():
            last_event.stop_event()

    # ============================================================
    # 内部工具
    # ============================================================

    def _append_context(self, state: GroupState, sender: str, text: str):
        state.conversation_context.append({"sender": sender, "text": text})
        if len(state.conversation_context) > 12:
            state.conversation_context = state.conversation_context[-12:]

    def _record_reply(self, state: GroupState, now: float, persona_name: str):
        state.last_reply_time = now
        state.reply_timestamps.append(now)
        window = self.config.get("debounce", {}).get("reply_window_seconds", 300)
        state.reply_timestamps = [t for t in state.reply_timestamps if now - t < window]
        decay = self.config.get("flow_engine", {}).get("flow_decay_on_reply", 20)
        old = state.flow_level
        state.flow_level = max(0, state.flow_level - decay)
        logger.debug(f"发言后心流 {old:.1f} → {state.flow_level:.1f}")

    async def _write_history(self, event: AstrMessageEvent, reply: str,
                             user_message: str = None):
        if not self.config.get("reply_engine", {}).get("record_conversation", True):
            return
        try:
            msg_text = user_message if user_message is not None else (event.message_str or "")
            mgr = self.context.conversation_manager
            cid = await mgr.get_curr_conversation_id(event.unified_msg_origin)
            if not cid:
                return
            await mgr.add_message_pair(
                cid=cid,
                user_message=UserMessageSegment(
                    content=[TextPart(text=msg_text)]
                ),
                assistant_message=AssistantMessageSegment(
                    content=[TextPart(text=reply)]
                ),
            )
        except Exception as e:
            logger.debug(f"写历史失败: {e}")

    # ============================================================
    # 指令
    # ============================================================

    @filter.command("mindflow", priority=1)
    async def cmd_mindflow(self, event: AstrMessageEvent):
        event.stop_event()
        gid = event.message_obj.group_id
        if not gid:
            yield event.plain_result("请在群聊中使用")
            return

        s = await self._get_state(gid)
        async with s.lock:
            flow = s.flow_level
            last = s.last_reply_time
            now = time.time()
            recent = len([t for t in s.reply_timestamps
                         if now - t < self.config.get("debounce", {}).get("reply_window_seconds", 300)])
            pending = len(s.pending_messages)

        if last > 0:
            ago = int(now - last)
            ago_s = f"{ago}s" if ago < 60 else f"{ago//60}min" if ago < 3600 else f"{ago//3600}h"
        else:
            ago_s = "━"

        bar = "█" * int(flow / 5) + "░" * (20 - int(flow / 5))
        yield event.plain_result(
            f"🤖 状态\n"
            f"心流 [{bar}] {flow:.0f}/100\n"
            f"上次: {ago_s} | 窗口: {recent}/{self.config.get('debounce', {}).get('max_replies_per_window', 6)}\n"
            f"缓冲: {pending}条"
        )

    @filter.command("mindflowreset", priority=1)
    async def cmd_reset(self, event: AstrMessageEvent):
        event.stop_event()
        gid = event.message_obj.group_id
        if not gid:
            yield event.plain_result("请在群聊中使用")
            return
        async with self._lock:
            init = float(self.config.get("flow_engine", {}).get("initial_flow", 50))
            old = self._states.get(gid)
            if old:
                self.accum.cancel_timer(old)
            self._states[gid] = GroupState(
                flow_level=init, last_update_time=time.time(),
            )
        yield event.plain_result(f"已重置，心流={init:.0f}/100")

    @filter.command("genkeywords", priority=1)
    async def cmd_genkeywords(self, event: AstrMessageEvent):
        """让 AI 根据当前人格生成感兴趣的关键词"""
        event.stop_event()
        if not self.config.get("reply_engine", {}).get("use_ai_keywords", False):
            yield event.plain_result("AI关键词生成未启用，请在插件配置中开启")
            return
        if self._keywords_generating:
            yield event.plain_result("⏳ 关键词正在生成中，请稍候...")
            return
        yield event.plain_result("🔄 正在根据人格设定生成关键词...")
        keywords = await self._gen_and_save_keywords(event)
        if keywords:
            yield event.plain_result(
                f"✅ 已生成 {len(keywords)} 个关键词：\n" +
                "\n".join(f"  • {kw}" for kw in keywords)
            )
        else:
            yield event.plain_result("❌ 关键词生成失败，请检查人格是否已配置")

    async def terminate(self):
        logger.info("拟人化群聊助手插件已卸载")
        for s in self._states.values():
            self.accum.cancel_timer(s)
        self._states.clear()
