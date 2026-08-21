import asyncio
import json
import time
from typing import Dict

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import AstrBotConfig
from astrbot.api import logger
from astrbot.api.web import json_response, error_response, request
from astrbot.core.agent.message import (
    AssistantMessageSegment,
    UserMessageSegment,
    TextPart,
)

from .engine import GroupState, FlowEngine, DebounceChecker, AccumulationManager
from .engine.flow import is_direct_mention, is_mentioned
from .engine.state import MAX_CONTEXT
from .ai import PersonaBridge, AIClient

PLUGIN_NAME = "astrbot_plugin_humanlike"

# 主动发言循环扫描间隔（秒）
PROACTIVE_SCAN_INTERVAL = 60
# 群状态保留时长：超过该时长无消息的群状态会被清理
STATE_TTL_SECONDS = 24 * 3600


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
        self._proactive_task = None
        # 入群时间记录（防爆破统计）：group_id -> [(时间戳, 用户ID)]
        self._join_records: Dict[str, list[tuple[float, str]]] = {}
        # AI 问答审核中的待验证成员：group_id -> {user_id: {asked_at, attempts, question}}
        self._pending_qa: Dict[str, Dict[str, dict]] = {}

        context.register_web_api(
            f"/{PLUGIN_NAME}/keywords/list", self._api_kw_list, ["GET"], "关键词列表")
        context.register_web_api(
            f"/{PLUGIN_NAME}/keywords/add", self._api_kw_add, ["POST"], "添加关键词")
        context.register_web_api(
            f"/{PLUGIN_NAME}/keywords/remove", self._api_kw_remove, ["POST"], "删除关键词")
        context.register_web_api(
            f"/{PLUGIN_NAME}/keywords/generate", self._api_kw_gen, ["POST"], "AI生成")
        context.register_web_api(
            f"/{PLUGIN_NAME}/keywords/clear", self._api_kw_clear, ["POST"], "清空关键词")
        context.register_web_api(
            f"/{PLUGIN_NAME}/keywords/personas", self._api_kw_personas, ["GET"], "人格列表")
        context.register_web_api(
            f"/{PLUGIN_NAME}/keywords/test", self._api_kw_test, ["POST"], "测试关键词")
        context.register_web_api(
            f"/{PLUGIN_NAME}/status/groups", self._api_status, ["GET"], "群聊状态")
        context.register_web_api(
            f"/{PLUGIN_NAME}/settings", self._api_settings_get, ["GET"], "读取全部设置")
        context.register_web_api(
            f"/{PLUGIN_NAME}/settings/save", self._api_settings_save, ["POST"], "保存设置")
        context.register_web_api(
            f"/{PLUGIN_NAME}/providers", self._api_providers, ["GET"], "可用的对话模型提供商")

        logger.info("拟人化群聊助手插件已加载")

    # ============================================================
    # 生命周期
    # ============================================================

    async def initialize(self):
        """插件激活时启动后台任务。"""
        self._proactive_task = asyncio.ensure_future(self._proactive_loop())
        await self._init_keywords()

    async def terminate(self):
        logger.info("拟人化群聊助手插件已卸载")
        if self._proactive_task:
            self._proactive_task.cancel()
            self._proactive_task = None
        for s in self._states.values():
            self.accum.cancel_timer(s)
        self._states.clear()
        self._join_records.clear()
        self._pending_qa.clear()

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
            logger.info("开始获取人格设定...")
            persona_prompt = await self.persona.system_prompt(event)
            persona_name = await self.persona.name(event)
            logger.info(f"人格: name={persona_name or '(无)'}, prompt长度={len(persona_prompt)}")
            if not persona_prompt:
                logger.warning("无人格设定，无法生成关键词")
                return []
            logger.info("开始调用 LLM 生成关键词...")
            count = max(3, min(30, int(self.config.get(
                "reply_engine", {}).get("keyword_count", 10))))
            keywords = await self.ai.generate_keywords(
                event, persona_prompt, persona_name, count)
            if keywords:
                self.flow.set_ai_keywords(keywords)
                await self.put_kv_data("ai_keywords", keywords)
                logger.info(f"AI生成并保存 {len(keywords)} 个关键词")
            return keywords
        except asyncio.TimeoutError:
            logger.error("关键词生成超时")
            return []
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

    def _wake_prefixes(self) -> list[str]:
        """读取 AstrBot 全局配置的指令唤醒前缀（默认 /）。

        其他插件用自定义前缀注册指令时，本插件也能正确放行，
        避免接管模式下吞掉其他插件的指令。
        """
        try:
            prefixes = (self.context.get_config().get("provider_settings", {})
                        or {}).get("wake_prefix", ["/"])
            if isinstance(prefixes, str):
                prefixes = [prefixes]
            result = [str(p) for p in prefixes if p]
            return result or ["/"]
        except Exception:
            return ["/"]

    def _is_command_message(self, event: AstrMessageEvent) -> bool:
        text = (event.message_str or "").strip()
        if not text:
            return False
        return any(text.startswith(p) for p in self._wake_prefixes())

    # ============================================================
    # 主入口
    # ============================================================

    @staticmethod
    def _raw_get(raw, key: str, default=None):
        """从原始事件对象取字段（兼容 dict / Mapping / pydantic 模型）。"""
        if raw is None:
            return default
        getter = getattr(raw, "get", None)
        if callable(getter):
            try:
                return getter(key, default)
            except Exception:
                pass
        return getattr(raw, key, default)

    def _detect_join_event(self, event: AstrMessageEvent) -> tuple[bool, str]:
        """判断消息事件是否为「新人入群」，返回 (是否入群, 新成员ID)。

        OneBot 系平台（aiocqhttp 等）：适配器把 notice 事件转为消息事件，
        原始数据保留在 message_obj.raw_message（post_type=notice,
        notice_type=group_increase）。其他平台回退到消息类型/文本特征。
        """
        raw = getattr(event.message_obj, "raw_message", None)
        post = str(self._raw_get(raw, "post_type", "") or "")
        ntype = str(self._raw_get(raw, "notice_type", "") or "")
        if post == "notice" and ntype == "group_increase":
            uid = str(self._raw_get(raw, "user_id", "") or "")
            return True, uid or str(event.get_sender_id())

        mtype = str(getattr(event.message_obj, "type", ""))
        msg_str = (event.message_str or "").lower()
        if any(k in mtype.lower() for k in
               ("increase", "member_add", "member_join", "group_member")):
            return True, str(event.get_sender_id())
        if any(k in msg_str for k in ("入群", "加入群聊", "member join",
                                      "joined the group")):
            return True, str(event.get_sender_id())
        return False, ""

    @staticmethod
    def _match_member(user_id: str, user_name: str, items: list) -> bool:
        """成员是否命中名单：QQ 号精确匹配，或昵称包含匹配。"""
        uid = user_id.strip()
        name = user_name.strip()
        for item in items or []:
            it = str(item).strip()
            if not it:
                continue
            if it == uid:
                return True
            if name and it.lower() in name.lower():
                return True
        return False

    def _record_join(self, group_id: str, ts: float, user_id: str):
        records = self._join_records.setdefault(group_id, [])
        records.append((ts, user_id))
        self._join_records[group_id] = [r for r in records if ts - r[0] <= 600][-200:]

    def _resolve_rule(self, group_id: str) -> dict:
        """解析该群生效的审核规则：精确群号优先匹配，`*` 兜底（最后匹配），
        命中的规则字段覆盖全局 group_manage 的同名参数。

        规则条目字段示例：
        {"match": "123456", "audit_mode": "ai_qa",
         "qa_question": "本群暗号？", "qa_audit_prompt": "..."}
        """
        gm = self.config.get("group_manage", {}) or {}
        merged = dict(gm)
        try:
            rules = [r for r in gm.get("group_rules", []) or []
                     if isinstance(r, dict)]
            # 第一轮：精确群号
            for rule in rules:
                if str(rule.get("match", "") or "").strip() == str(group_id):
                    merged.update({k: v for k, v in rule.items() if k != "match"})
                    return merged
            # 第二轮：* 兜底
            for rule in rules:
                if str(rule.get("match", "") or "").strip() == "*":
                    merged.update({k: v for k, v in rule.items() if k != "match"})
                    break
        except Exception as e:
            logger.debug(f"解析群规则失败: {e}")
        return merged

    async def _reject_member(self, event: AstrMessageEvent, group_id: str,
                             user_id: str, user_name: str, reason: str):
        """审核不通过：踢出 + 可选通知。"""
        gm = self.config.get("group_manage", {}) or {}
        kicked = await self._kick_member(event, group_id, user_id)
        if gm.get("kick_notice", True):
            if kicked:
                template = gm.get("kick_message", "已拒绝 {user_name} 入群。")
                text = str(template).replace("{user_name}", user_name)
                await self._send_group(event, text)
            else:
                await self._send_group(
                    event,
                    f"⚠️ {user_name} 未通过入群审核，但当前平台不支持自动移除，请管理员处理。")
        event.stop_event()
        logger.info(
            f"[群:{group_id}] 已拒绝 {user_name}({user_id}) 入群: {reason}"
            f"（踢出={'成功' if kicked else '失败/平台不支持'}）"
        )

    async def _kick_member(self, event: AstrMessageEvent,
                           group_id: str, user_id: str) -> bool:
        """踢出群成员（仅支持 OneBot 系平台，如 aiocqhttp）。"""
        try:
            platform = self.context.get_platform_inst(event.get_platform_id())
            bot = platform.get_client() if platform else None
            if bot is None or not hasattr(bot, "call_action"):
                logger.warning(f"[群:{group_id}] 当前平台不支持踢人操作")
                return False
            await bot.call_action(
                "set_group_kick",
                group_id=int(group_id),
                user_id=int(user_id),
                reject_add_request=False,
            )
            return True
        except Exception as e:
            logger.warning(f"[群:{group_id}] 踢人失败: {e}")
            return False

    async def _send_group(self, event: AstrMessageEvent, text: str):
        try:
            from astrbot.core.message.message_event_result import MessageChain
            await self.context.send_message(
                session=event.unified_msg_origin,
                message_chain=MessageChain().message(text),
            )
        except Exception as e:
            logger.debug(f"群消息发送失败: {e}")

    async def _get_member_level(self, event: AstrMessageEvent,
                                group_id: str, user_id: str) -> int | None:
        """查询成员群等级（OneBot 系 get_group_member_info.level）。

        返回数字等级；平台不支持/查询失败返回 None。
        """
        try:
            platform = self.context.get_platform_inst(event.get_platform_id())
            bot = platform.get_client() if platform else None
            if bot is None or not hasattr(bot, "call_action"):
                logger.warning(f"[群:{group_id}] 当前平台不支持等级查询")
                return None
            info = await bot.call_action(
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(user_id),
            )
            level = (info or {}).get("level", "")
            return int(str(level).strip()) if str(level).strip().isdigit() else None
        except Exception as e:
            logger.warning(f"[群:{group_id}] 查询成员等级失败: {e}")
            return None

    # ── AI 问答审核 ──────────────────────────────────────────

    async def _start_qa(self, event: AstrMessageEvent, group_id: str,
                        user_id: str, user_name: str, rule: dict):
        """入群后发起 AI 问答验证：提问并登记待验证状态。"""
        question = str(rule.get("qa_question", "") or "").strip()
        if not question:
            persona_prompt = (await self.persona.system_prompt_for(
                event.unified_msg_origin)) if self.persona.enabled else ""
            persona_name = (await self.persona.name_for(
                event.unified_msg_origin)) if self.persona.enabled else ""
            question = await self.ai.generate_qa_question(
                event.unified_msg_origin,
                str(rule.get("qa_audit_prompt", "") or ""),
                persona_prompt, persona_name)
        if not question:
            question = "请回答本群的入群验证问题：你是因为什么加入本群的？"

        self._pending_qa.setdefault(group_id, {})[user_id] = {
            "asked_at": time.time(),
            "attempts": 0,
            "question": question,
            "user_name": user_name,
            "platform_id": event.get_platform_id(),
        }
        try:
            from astrbot.core.message.message_event_result import MessageChain
            chain = MessageChain()
            try:
                chain.at(user_name, int(user_id))
            except Exception:
                chain.message(f"@{user_name} ")
            chain.message(f"{question}\n"
                         f"（请在群里回复答案，共{rule.get('qa_max_attempts', 3)}次机会）")
            await self.context.send_message(
                session=event.unified_msg_origin, message_chain=chain)
        except Exception as e:
            logger.debug(f"发送验证问题失败: {e}")
        event.stop_event()
        logger.info(f"[群:{group_id}] 已向 {user_name}({user_id}) 发起入群问答")

    async def _handle_qa_reply(self, event: AstrMessageEvent, group_id: str,
                               user_id: str, rule: dict):
        """待验证成员发言：AI 审核回答。"""
        pend = self._pending_qa.get(group_id, {}).get(user_id)
        if not pend:
            return
        answer = (event.message_str or "").strip()
        user_name = pend.get("user_name") or event.get_sender_name() or "新成员"
        pend["attempts"] += 1
        max_attempts = int(rule.get("qa_max_attempts", 3))
        event.stop_event()

        if not answer:
            left = max_attempts - pend["attempts"]
            await self._send_group(event,
                                   f"{user_name} 请直接回复文字答案（还可回答{left}次）")
            return

        persona_prompt = (await self.persona.system_prompt_for(
            event.unified_msg_origin)) if self.persona.enabled else ""
        persona_name = (await self.persona.name_for(
            event.unified_msg_origin)) if self.persona.enabled else ""
        passed, reason = await self.ai.audit_qa_answer(
            event.unified_msg_origin, pend["question"], answer,
            str(rule.get("qa_audit_prompt", "") or ""),
            persona_prompt, persona_name)
        self._pending_qa.get(group_id, {}).pop(user_id, None)

        if passed:
            if rule.get("welcome_enabled", False):
                template = rule.get("welcome_message",
                                    "欢迎 {user_name} 加入本群！")
                group_name = getattr(event.message_obj, "group_name", "") or ""
                text = (str(template).replace("{user_name}", user_name)
                        .replace("{group_name}", str(group_name)))
                await self._send_group(event, text)
            logger.info(f"[群:{group_id}] 问答通过: {user_name}({user_id})")
        else:
            await self._reject_member(event, group_id, user_id, user_name,
                                      f"问答不通过: {reason}")
        self._prune_qa_group(group_id)

    def _prune_qa_group(self, group_id: str):
        pend = self._pending_qa.get(group_id)
        if pend is not None and not pend:
            self._pending_qa.pop(group_id, None)

    async def _expire_qa(self, group_id: str, now: float):
        """超时未通过验证的待验证成员：拒绝（有踢人能力的平台踢出）。"""
        pend = self._pending_qa.get(group_id)
        if not pend:
            return
        timeout = float(self.config.get("group_manage", {}).get(
            "qa_timeout_minutes", 10)) * 60
        stale = [(uid, p) for uid, p in pend.items()
                 if now - p["asked_at"] > timeout]
        for uid, p in stale:
            pend.pop(uid, None)
            logger.info(f"[群:{group_id}] 问答超时，拒绝 {p.get('user_name') or uid}({uid})")
            # 超时场景没有可用的入群事件，跳过群通知，仅尽力踢出
            try:
                platform = self.context.get_platform_inst(
                    p.get("platform_id", "aiocqhttp"))
                bot = platform.get_client() if platform else None
                if bot is not None and hasattr(bot, "call_action"):
                    await bot.call_action(
                        "set_group_kick",
                        group_id=int(group_id),
                        user_id=int(uid),
                        reject_add_request=False,
                    )
            except Exception as e:
                logger.debug(f"[群:{group_id}] 超时踢出失败: {e}")
        self._prune_qa_group(group_id)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_member_join(self, event: AstrMessageEvent):
        """入群事件处理：自动审核（名单/AI问答/等级）+ 防爆破 + 欢迎。

        与主消息处理器共存：入群事件由本方法处理，
        主处理器会因「无文本消息」忽略它。
        """
        gm = self.config.get("group_manage", {}) or {}
        if not gm.get("enabled", False):
            return
        group_id = event.message_obj.group_id
        if not group_id:
            return
        is_join, user_id = self._detect_join_event(event)
        if not is_join:
            return

        user_name = event.get_sender_name() or "新成员"
        now = time.time()
        # 惰性清理本群过期的问答待验证成员
        await self._expire_qa(group_id, now)
        self._record_join(group_id, now, user_id)
        logger.info(f"[群:{group_id}] 检测到入群: {user_name}({user_id})")

        # 解析该群生效的审核规则（group_rules 覆盖全局）
        rule = self._resolve_rule(group_id)

        # 防爆破：窗口内入群人数达到阈值 → 窗口内成员全部拒绝（含已放行的）
        if rule.get("anti_raid_enabled", True):
            window = float(rule.get("anti_raid_window", 60))
            records = [r for r in self._join_records.get(group_id, [])
                       if now - r[0] < window]
            threshold = int(rule.get("anti_raid_count", 5))
            if len(records) >= threshold:
                logger.warning(
                    f"[群:{group_id}] 防爆破触发: {window:.0f}s内{len(records)}人入群"
                )
                # 追踢窗口内已放行的爆破成员（幂等，重复踢无副作用）
                for ts, uid in records:
                    if uid != user_id:
                        await self._kick_member(event, group_id, uid)
                await self._reject_member(event, group_id, user_id,
                                          user_name, "防爆破")
                return

        # 名单审核：黑名单在所有模式下都优先拒绝
        allow_list = rule.get("allow_list", []) or []
        block_list = rule.get("block_list", []) or []
        blocked = self._match_member(user_id, user_name, block_list)
        allowed = self._match_member(user_id, user_name, allow_list)
        if blocked:
            await self._reject_member(event, group_id, user_id,
                                      user_name, "黑名单")
            return

        mode = str(rule.get("audit_mode", "off"))
        if mode == "blacklist":
            # 黑名单已在上面拦截，其余放行
            pass
        elif mode == "strict" and not allowed:
            await self._reject_member(event, group_id, user_id,
                                      user_name, "不在白名单")
            return
        elif mode == "ai_qa":
            if allowed:
                # 白名单成员免问答
                pass
            else:
                await self._start_qa(event, group_id, user_id,
                                     user_name, rule)
                return
        elif mode == "level":
            if allowed:
                pass
            else:
                level = await self._get_member_level(event, group_id, user_id)
                if level is None:
                    unknown = str(rule.get("level_unknown_action", "allow"))
                    if unknown == "reject":
                        await self._reject_member(event, group_id, user_id,
                                                  user_name, "等级未知")
                        return
                    logger.warning(
                        f"[群:{group_id}] 无法获取 {user_name}({user_id}) 的等级，"
                        f"按配置放行")
                elif level < int(rule.get("level_min", 3)):
                    await self._reject_member(event, group_id, user_id,
                                              user_name, f"等级过低({level})")
                    return

        # 审核通过：欢迎
        if rule.get("welcome_enabled", False):
            template = rule.get("welcome_message", "欢迎 {user_name} 加入本群！")
            group_name = getattr(event.message_obj, "group_name", "") or ""
            text = (str(template)
                    .replace("{user_name}", user_name)
                    .replace("{group_name}", str(group_name)))
            await self._send_group(event, text)
            logger.info(f"[群:{group_id}] 欢迎新人: {user_name}")
        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        group_id = event.message_obj.group_id
        if not group_id:
            return

        sender_id = str(event.get_sender_id())
        if sender_id == str(event.message_obj.self_id):
            return

        # AI 问答审核：待验证成员的发言走审核流程，不进入拟人对话
        gm = self.config.get("group_manage", {}) or {}
        if gm.get("enabled", False) and self._pending_qa.get(group_id, {}).get(sender_id):
            rule = self._resolve_rule(group_id)
            await self._handle_qa_reply(event, group_id, sender_id, rule)
            return

        cfg = self.config
        msg_preview = (event.message_str or "")[:50]

        if cfg.get("reply_engine", {}).get("allow_command_pass", True):
            if self._is_command_message(event):
                logger.debug(f"[群:{group_id}] 指令放行: {msg_preview}")
                return

        logger.debug(
            f"[群:{group_id}] 收到消息 | "
            f"{event.get_sender_name()}({sender_id}) | {msg_preview}"
        )

        state = await self._get_state(group_id)
        # 被直接点名 → 必定回复：跳过 AI 判断与防抖（@全体不算点名）
        mentioned = is_direct_mention(event)
        msg_text = event.message_str or ""

        if not mentioned and not msg_text.strip():
            # 纯图片/表情/语音等无文本消息：不参与对话、不触发 AI
            logger.debug(f"[群:{group_id}] 无文本消息，忽略")
            self._stop_if_override(event)
            return

        persona_prompt, persona_name = "", ""
        if self.persona.enabled:
            persona_prompt = await self.persona.system_prompt(event)
            persona_name = await self.persona.name(event)

        if cfg.get("reply_engine", {}).get("use_ai_keywords", False):
            if (self.flow.has_ai_keywords is False and self._keywords_loaded
                    and not self._keywords_generating):
                asyncio.ensure_future(self._gen_and_save_keywords(event))

        now = time.time()

        async with state.lock:
            # 记录会话来源（主动发言发送消息需要）与群消息时间（活跃度统计需要）
            if not state.unified_msg_origin and event.unified_msg_origin:
                state.unified_msg_origin = event.unified_msg_origin
            state.last_msg_time = now
            state.record_msg_time(now)

            old_flow = state.flow_level

            same_speaker = (state.last_speaker_id == sender_id)
            if same_speaker:
                state.same_speaker_count += 1
            else:
                state.same_speaker_count = 1
            state.last_speaker_id = sender_id

            self.flow.update(state, event, msg_text, now, persona_name)

            if state.same_speaker_count >= 3:
                penalty = min(10, (state.same_speaker_count - 2) * 3)
                state.flow_level = max(0, state.flow_level - penalty)
                logger.debug(
                    f"[群:{group_id}] 同一人连续{state.same_speaker_count}条 → 心流-{penalty}"
                )

            time_since_reply = now - state.last_reply_time
            if 0 < time_since_reply < 30 and state.last_reply_time > 0:
                if persona_name and persona_name in msg_text:
                    boost = 15
                    state.flow_level = min(100, state.flow_level + boost)
                    logger.debug(
                        f"[群:{group_id}] 回复互动检测 → 心流+{boost}"
                    )

            if state.flow_level != old_flow:
                logger.debug(
                    f"[群:{group_id}] 心流 {old_flow:.1f} → {state.flow_level:.1f}"
                )

            immediate = self.accum.is_immediate_trigger(
                event, state.flow_level, persona_name
            )

            if immediate and state.reply_in_progress:
                # 已有回复正在生成：本条转为累积等待（或直接忽略），
                # 防止同群两条几乎同时的立即触发消息并发生成两条回复
                if not self.accum.enabled:
                    self._stop_if_override(event)
                    return
                immediate = False

            if immediate:
                self.accum.cancel_timer(state)
                if not state.pipeline_running:
                    # 批处理管道空闲：把缓冲消息并入上下文后立即处理
                    for m in state.pending_messages:
                        state.conversation_context.append({
                            "sender": m["sender"], "text": m["text"],
                        })
                    state.pending_messages.clear()
                    if len(state.conversation_context) > MAX_CONTEXT:
                        state.conversation_context = (
                            state.conversation_context[-MAX_CONTEXT:])
                # 若管道正在运行，缓冲消息由管道负责写入上下文，避免重复
                state.append_context(event.get_sender_name() or "未知", msg_text)
                if not mentioned and not self.debounce.check(state, now):
                    logger.debug(f"[群:{group_id}] 防抖拦截（立即触发但频率受限）")
                    self._stop_if_override(event)
                    return

                # 防抖通过后才占位，防止防抖失败 return 时标志泄漏
                state.reply_in_progress = True
                flow_snap = state.flow_level
                ctx_snap = list(state.conversation_context[-8:])

            else:
                if self.accum.enabled:
                    self.accum.add_to_buffer(state, event, msg_text,
                                             event.get_sender_name() or "未知")
                    state.retry_count = 0
                    self.accum.cancel_timer(state)
                    await self.accum.start_timer(group_id, state,
                                                 self._on_silence_timeout)

                    if self.accum.should_force_process(state):
                        logger.debug(f"[群:{group_id}] 累积缓冲满，立即处理")
                        self.accum.cancel_timer(state)
                        asyncio.ensure_future(self._run_batch_pipeline(group_id))

                    logger.debug(
                        f"[群:{group_id}] 累积: 缓冲({len(state.pending_messages)}条, "
                        f"心流={state.flow_level:.0f})"
                    )
                else:
                    state.append_context(event.get_sender_name() or "未知",
                                         msg_text)
                    if not mentioned and not self.debounce.check(state, now):
                        logger.debug(f"[群:{group_id}] 防抖拦截")
                        self._stop_if_override(event)
                        return
                    flow_snap = state.flow_level
                    ctx_snap = list(state.conversation_context[-8:])
                    immediate = True  # 走立即处理路径

                self._stop_if_override(event)
                if self.accum.enabled:
                    return

        await self._run_immediate(event, state, flow_snap, ctx_snap,
                                  persona_prompt, persona_name, mentioned)

    # ============================================================
    # 立即处理
    # ============================================================

    async def _run_immediate(self, event, state, flow_snap, ctx_snap,
                             persona_prompt, persona_name, mentioned=False):
        try:
            if mentioned:
                logger.info("被@提及 → 必定回复（跳过AI判断）")
            else:
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
                state.append_context(
                    persona_name or self.config.get("reply_engine", {}).get("bot_name", "bot"),
                    reply,
                )

            await self._write_history(event, reply)
            self._stop_if_override(event)
        finally:
            # 无论成功/沉默/异常，都释放立即回复互斥标志
            async with state.lock:
                state.reply_in_progress = False

    # ============================================================
    # 累积批处理
    # ============================================================

    async def _on_silence_timeout(self, group_id: str):
        await self._run_batch_pipeline(group_id)

    async def _start_retry_timer(self, group_id: str, state: GroupState):

        async def _retry():
            await asyncio.sleep(20)
            await self._run_batch_pipeline(group_id)

        self.accum.cancel_timer(state)
        state.silence_timer = asyncio.ensure_future(_retry())

    async def _run_batch_pipeline(self, group_id: str):
        state = await self._get_state(group_id)

        # pipeline_running 守卫：防止计时器、缓冲满、重试等多路并发触发
        # 导致同一批消息被处理两次（重复回复）
        async with state.lock:
            if state.pipeline_running or not state.pending_messages:
                return
            if state.reply_in_progress:
                # 立即回复正在生成：批处理延迟，避免同一时刻两条回复
                logger.debug(f"[群:{group_id}] 立即回复进行中，批处理延迟20s")
                await self._start_retry_timer(group_id, state)
                return
            state.pipeline_running = True
            pending = list(state.pending_messages)

        try:
            last_event = pending[-1].get("event") if pending else None
            if not last_event:
                return
            # 批处理中若最后一条是直接点名（如防抖重试期间积压的 @），同样必定回复
            mentioned = is_direct_mention(last_event)

            logger.debug(
                f"[群:{group_id}] 批处理: {len(pending)}条 "
                f"| {' → '.join(m['text'][:20] for m in pending[-3:])}"
            )

            now = time.time()
            if mentioned:
                state.retry_count = 0
            else:
                async with state.lock:
                    debounce_ok = self.debounce.check(state, now)
                    if not debounce_ok:
                        state.retry_count += 1
                        if state.retry_count > 2:
                            logger.info(f"[群:{group_id}] 批处理: 重试{state.retry_count}次，跳过防抖直接交AI判断")
                        else:
                            logger.info(f"[群:{group_id}] 批处理: 防抖拦截({state.retry_count}/3)，20s后重试")
                            await self._start_retry_timer(group_id, state)
                            return
                    else:
                        state.retry_count = 0

            async with state.lock:
                # 只移除本次已拷贝的消息：处理期间新缓冲的消息不能丢
                del state.pending_messages[:len(pending)]
                state.silence_timer = None
                for m in pending:
                    state.conversation_context.append({
                        "sender": m["sender"], "text": m["text"],
                    })
                if len(state.conversation_context) > MAX_CONTEXT:
                    state.conversation_context = state.conversation_context[-MAX_CONTEXT:]

            last_event = pending[-1].get("event") if pending else None
            if not last_event:
                return

            persona_prompt, persona_name = "", ""
            if self.persona.enabled:
                persona_prompt = await self.persona.system_prompt(last_event)
                persona_name = await self.persona.name(last_event)

            ctx_list = list(state.conversation_context[-10:])
            flow_snap = state.flow_level

            if mentioned:
                logger.info(f"[群:{group_id}] 批处理: 被@提及 → 必定回复（跳过AI判断）")
            elif not await self.ai.judge_batch(last_event, flow_snap, ctx_list,
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
                state.append_context(
                    persona_name or self.config.get("reply_engine", {}).get("bot_name", "bot"),
                    reply,
                )

            await last_event.send(last_event.plain_result(reply))

            combined_user = " | ".join([m["text"] for m in pending])
            await self._write_history(last_event, reply, user_message=combined_user)

            if self._override():
                last_event.stop_event()
        finally:
            async with state.lock:
                state.pipeline_running = False

    # ============================================================
    # 主动发言
    # ============================================================

    async def _cleanup_stale_states(self):
        """清理超过 STATE_TTL_SECONDS 无消息的群状态，防止内存无限增长。"""
        try:
            cutoff = time.time() - STATE_TTL_SECONDS
            async with self._lock:
                stale = [gid for gid, s in self._states.items()
                         if s.last_msg_time > 0 and s.last_msg_time < cutoff]
                for gid in stale:
                    s = self._states[gid]
                    self.accum.cancel_timer(s)
                    del self._states[gid]
            if stale:
                logger.debug(f"清理 {len(stale)} 个超过24h无消息的群状态")
        except Exception as e:
            logger.debug(f"清理群状态失败: {e}")

    async def _proactive_loop(self):
        """周期性扫描各群，冷场到一定程度时让 AI 决定是否主动起话题。"""
        while True:
            try:
                await asyncio.sleep(PROACTIVE_SCAN_INTERVAL)
                await self._cleanup_stale_states()
                pc = self.config.get("proactive", {}) or {}
                if not pc.get("enabled", False):
                    continue
                if not self._override():
                    continue
                now = time.time()
                async with self._lock:
                    items = list(self._states.items())
                for gid, state in items:
                    try:
                        await self._try_proactive(gid, state, now)
                    except Exception as e:
                        logger.debug(f"[群:{gid}] 主动发言尝试失败: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"主动发言循环异常: {e}")

    async def _try_proactive(self, gid: str, state: GroupState, now: float):
        pc = self.config.get("proactive", {}) or {}
        idle_min = float(pc.get("idle_minutes", 30))
        cooldown_min = float(pc.get("cooldown_minutes", 60))
        min_flow = float(pc.get("min_flow", 40))
        max_per_day = int(pc.get("max_per_day", 3))

        if not state.unified_msg_origin:
            return
        if now - state.last_msg_time < idle_min * 60:
            return
        if state.proactive_timestamps:
            if now - state.proactive_timestamps[-1] < cooldown_min * 60:
                return
        recent = [t for t in state.proactive_timestamps if now - t < 86400]
        if len(recent) >= max_per_day:
            return
        if state.flow_level < min_flow:
            return

        async with state.lock:
            if state.pipeline_running:
                return
            state.pipeline_running = True
        try:
            ctx_list = list(state.conversation_context[-6:])
            umo = state.unified_msg_origin
            persona_prompt, persona_name = "", ""
            if self.persona.enabled:
                persona_prompt = await self.persona.system_prompt_for(umo)
                persona_name = await self.persona.name_for(umo)

            text = await self.ai.proactive_topic(
                umo, idle_min, ctx_list, persona_prompt, persona_name)
            if not text:
                return

            # 发送前复查：AI 生成话题的这几秒内群若已恢复活跃，取消发言
            async with state.lock:
                if time.time() - state.last_msg_time < idle_min * 60:
                    logger.debug(f"[群:{gid}] 主动发言取消：生成期间群已活跃")
                    return

            try:
                from astrbot.core.message.message_event_result import MessageChain
                ok = await self.context.send_message(
                    session=umo, message_chain=MessageChain().message(text))
            except Exception as e:
                logger.warning(f"[群:{gid}] 主动发言发送失败: {e}")
                ok = False
            if not ok:
                logger.warning(f"[群:{gid}] 主动发言发送失败（平台不支持）")
                # 失败也记录时间戳进入冷却，避免每轮扫描（60s）重复尝试
                async with state.lock:
                    state.proactive_timestamps.append(time.time())
                    state.proactive_timestamps = [
                        t for t in state.proactive_timestamps
                        if time.time() - t < 86400
                    ]
                return

            async with state.lock:
                state.proactive_timestamps.append(time.time())
                state.proactive_timestamps = [
                    t for t in state.proactive_timestamps if time.time() - t < 86400
                ]
                self._record_reply(state, now=time.time(),
                                   persona_name=persona_name)
                state.append_context(
                    persona_name or self.config.get("reply_engine", {}).get("bot_name", "bot"),
                    text,
                )
            logger.info(f"[群:{gid}] 主动发言: {text[:40]}")
        finally:
            async with state.lock:
                state.pipeline_running = False

    # ============================================================
    # 内部工具
    # ============================================================

    def _record_reply(self, state: GroupState, now: float, persona_name: str):
        state.last_reply_time = now
        state.reply_timestamps.append(now)
        window = self.config.get("debounce", {}).get("reply_window_seconds", 300)
        state.reply_timestamps = [t for t in state.reply_timestamps if now - t < window]
        decay = self.config.get("flow_engine", {}).get("flow_decay_on_reply", 12)
        old = state.flow_level
        state.flow_level = max(0, state.flow_level - decay)
        # 机器人发言后，同人连续发言计数归零（新的一轮）
        state.same_speaker_count = 0
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
            proactive_on = self.config.get("proactive", {}).get("enabled", False)
            proactive_cnt = len([t for t in s.proactive_timestamps if now - t < 86400])
            max_win = self.config.get("debounce", {}).get("max_replies_per_window", 12)

        if last > 0:
            ago = int(now - last)
            ago_s = f"{ago}s" if ago < 60 else f"{ago//60}min" if ago < 3600 else f"{ago//3600}h"
        else:
            ago_s = "━"

        bar = "█" * int(flow / 5) + "░" * (20 - int(flow / 5))
        lines = [
            f"🤖 状态",
            f"心流 [{bar}] {flow:.0f}/100",
            f"上次: {ago_s} | 窗口: {recent}/{max_win}",
            f"缓冲: {pending}条",
        ]
        if proactive_on:
            lines.append(f"主动发言: 今日{proactive_cnt}次")
        yield event.plain_result("\n".join(lines))

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

    @filter.command("keywords", priority=1)
    async def cmd_keywords(self, event: AstrMessageEvent):
        event.stop_event()
        manual = self.config.get("interest_keywords", []) or []
        ai_kw = list(self.flow._ai_keywords) if hasattr(self.flow, '_ai_keywords') else []
        merged = list(self.flow._all_keywords()) if hasattr(self.flow, '_all_keywords') else manual

        lines = [f"📋 当前关键词（共{len(merged)}个）："]
        if manual:
            lines.append(f"\n手动配置({len(manual)}): {', '.join(manual)}")
        if ai_kw:
            lines.append(f"\nAI生成({len(ai_kw)}): {', '.join(ai_kw)}")
        if not manual and not ai_kw:
            lines.append("\n暂无关键词，使用 /genkeywords 生成")
        yield event.plain_result("".join(lines))

    @filter.command("genkeywords", priority=1)
    async def cmd_genkeywords(self, event: AstrMessageEvent):
        """让 AI 根据当前人格生成感兴趣的关键词"""
        event.stop_event()
        if self._keywords_generating:
            await event.send(event.plain_result("⏳ 关键词正在生成中，请稍候..."))
            return
        await event.send(event.plain_result("🔄 正在根据人格设定生成关键词..."))
        keywords = await self._gen_and_save_keywords(event)
        if keywords:
            enabled = self.config.get("reply_engine", {}).get("use_ai_keywords", False)
            tip = "" if enabled else "\n（提示：AI关键词生成未启用，生成的关键词暂不参与匹配）"
            await event.send(event.plain_result(
                f"✅ 已生成 {len(keywords)} 个关键词：\n" +
                "\n".join(f"  • {kw}" for kw in keywords) + tip
            ))
        else:
            await event.send(event.plain_result("❌ 关键词生成失败，请检查人格是否已配置"))

    # ============================================================
    # Web API — 关键词管理
    # ============================================================

    async def _api_kw_list(self):
        manual = (self.config.get("interest_keywords", []) or [])[:]
        ai_kw = list(self.flow._ai_keywords) if hasattr(self.flow, '_ai_keywords') else []
        return json_response({"manual": manual, "ai": ai_kw, "all": manual + ai_kw})

    async def _api_kw_add(self):
        body = await request.json(default={})
        kw = (body.get("keyword") or "").strip()
        if not kw:
            return error_response("关键词不能为空")
        manual = self.config.get("interest_keywords", []) or []
        if kw in manual:
            return json_response({"ok": True, "existed": True})
        manual.append(kw)
        self.config["interest_keywords"] = manual
        try:
            self.config.save_config()
        except Exception:
            pass
        self.flow._interest_keywords = manual
        return json_response({"ok": True, "keyword": kw})

    async def _api_kw_remove(self):
        body = await request.json(default={})
        kw = (body.get("keyword") or "").strip()
        manual = self.config.get("interest_keywords", []) or []
        if kw not in manual:
            return json_response({"ok": False, "message": "关键词不存在"})
        manual.remove(kw)
        self.config["interest_keywords"] = manual
        try:
            self.config.save_config()
        except Exception:
            pass
        self.flow._interest_keywords = manual
        return json_response({"ok": True})

    async def _resolve_persona(self, persona_id: str) -> tuple[str, str]:
        """按 id 解析人格设定，返回 (prompt, name)。

        优先新版 v3 人格（按 name），回退旧版 v2 人格（按 id），
        最后回退当前默认人格。
        """
        pm = self.context.persona_manager
        if persona_id:
            try:
                if hasattr(pm, "get_persona_v3_by_id"):
                    p3 = pm.get_persona_v3_by_id(persona_id)
                    if p3 and p3.get("prompt"):
                        return (str(p3["prompt"]).strip(),
                                str(p3.get("name", "") or "").strip())
            except Exception:
                pass
            try:
                p2 = await pm.get_persona(persona_id)
                return ((getattr(p2, "system_prompt", "") or "").strip(),
                        (getattr(p2, "persona_id", "") or "").strip())
            except Exception:
                pass
        try:
            p = await pm.get_default_persona_v3(None)
            if p and p.get("prompt"):
                return str(p["prompt"]).strip(), str(p.get("name", "") or "").strip()
        except Exception:
            pass
        return "", ""

    async def _api_kw_personas(self):
        try:
            pm = self.context.persona_manager
            result = []
            # 新版 v3 人格
            try:
                for p in pm.personas_v3 or []:
                    name = (p.get("name") or "").strip()
                    prompt = (p.get("prompt") or "").strip()
                    if name and prompt and not any(r["id"] == name for r in result):
                        result.append({"id": name, "preview": prompt[:80]})
            except Exception:
                pass
            # 旧版 v2 人格
            try:
                for p in pm.personas or []:
                    pid = (getattr(p, "persona_id", "") or "").strip()
                    prompt = (getattr(p, "system_prompt", "") or "").strip()
                    if pid and prompt and not any(r["id"] == pid for r in result):
                        result.append({"id": pid, "preview": prompt[:80]})
            except Exception:
                pass
            if not result:
                p = await pm.get_default_persona_v3(None)
                result = [{"id": p.get("name", "default"),
                           "preview": p.get("prompt", "")[:80]}]
            return json_response(result)
        except Exception as e:
            return error_response(str(e))

    async def _api_kw_gen(self):
        if self._keywords_generating:
            return error_response("关键词正在生成中")
        self._keywords_generating = True
        try:
            body = await request.json(default={})
            persona_id = (body.get("persona_id") or "").strip()
            persona_prompt, persona_name = await self._resolve_persona(persona_id)
            if not persona_prompt:
                return error_response("无人格设定，请先在人格管理中创建人格")

            count = max(3, min(30, int(self.config.get(
                "reply_engine", {}).get("keyword_count", 10))))
            keywords = await self.ai.generate_keywords(
                None, persona_prompt, persona_name, count, umo=None)
            if not keywords:
                return error_response("生成失败，请检查模型提供商配置")

            self.flow.set_ai_keywords(keywords)
            await self.put_kv_data("ai_keywords", keywords)
            logger.info(f"WebUI生成关键词 ({len(keywords)}个): {keywords[:10]}")
            return json_response({"ok": True, "keywords": keywords})
        except asyncio.TimeoutError:
            return error_response("生成超时")
        except Exception as e:
            return error_response(str(e))
        finally:
            self._keywords_generating = False

    async def _api_kw_clear(self):
        body = await request.json(default={})
        target = body.get("target", "manual")
        if target == "manual":
            self.config["interest_keywords"] = []
            try:
                self.config.save_config()
            except Exception:
                pass
            self.flow._interest_keywords = []
        elif target == "ai":
            self.flow.set_ai_keywords([])
            await self.delete_kv_data("ai_keywords")
        return json_response({"ok": True})

    async def _api_kw_test(self):
        body = await request.json(default={})
        text = (body.get("text") or "").strip().lower()
        if not text:
            return json_response({"hits": []})
        all_kw = list(self.flow._all_keywords()) if hasattr(self.flow, '_all_keywords') else []
        hits = []
        for kw in all_kw:
            if kw and kw.lower() in text:
                hits.append(kw)
        return json_response({"hits": hits, "total_keywords": len(all_kw)})

    # ============================================================
    # Web API — 状态
    # ============================================================

    async def _api_status(self):
        now = time.time()
        groups = []
        async with self._lock:
            state_items = list(self._states.items())
        for gid, s in state_items:
            async with s.lock:
                win = self.config.get("debounce", {}).get("reply_window_seconds", 300)
                max_msgs = self.config.get("debounce", {}).get("max_replies_per_window", 12)
                recent = len([t for t in s.reply_timestamps if now - t < win])
                last_ago = int(now - s.last_reply_time) if s.last_reply_time > 0 else -1
                last_msg_ago = int(now - s.last_msg_time) if s.last_msg_time > 0 else -1
                proactive_cnt = len([t for t in s.proactive_timestamps if now - t < 86400])
                groups.append({
                    "group_id": gid[:20],
                    "flow": s.flow_level,
                    "last_reply_ago": last_ago,
                    "last_msg_ago": last_msg_ago,
                    "recent_replies": f"{recent}/{max_msgs}",
                    "pending": len(s.pending_messages),
                    "context_len": len(s.conversation_context),
                    "proactive_today": proactive_cnt,
                })
        groups.sort(key=lambda g: -g["flow"])
        pc = self.config.get("proactive", {}) or {}
        return json_response({
            "total_groups": len(groups),
            "groups": groups,
            "override": self._override(),
            "accum_enabled": self.accum.enabled,
            "proactive_enabled": pc.get("enabled", False),
        })

    # ============================================================
    # Web API — 设置
    # ============================================================

    async def _api_settings_get(self):
        return json_response({
            "reply_engine": dict(self.config.get("reply_engine", {})),
            "flow_engine": dict(self.config.get("flow_engine", {})),
            "debounce": dict(self.config.get("debounce", {})),
            "accumulation": dict(self.config.get("accumulation", {})),
            "proactive": dict(self.config.get("proactive", {})),
            "ai_timeout": dict(self.config.get("ai_timeout", {})),
            "group_manage": dict(self.config.get("group_manage", {})),
            "interest_keywords": self.config.get("interest_keywords", []) or [],
            "reply_style": self.config.get("reply_style", ""),
            "ai_judge_prompt": self.config.get("ai_judge_prompt", ""),
            "ai_reply_prompt": self.config.get("ai_reply_prompt", ""),
        })

    async def _api_settings_save(self):
        body = await request.json(default={})
        changed = False

        # 名单/规则类字段：Dashboard 以多行文本/JSON 提交，先转换再合并
        if isinstance(body.get("group_manage"), dict):
            for key in ("allow_list", "block_list"):
                if key in body["group_manage"]:
                    val = body["group_manage"][key]
                    if isinstance(val, str):
                        body["group_manage"][key] = [
                            ln.strip() for ln in val.splitlines() if ln.strip()
                        ]
                    elif not isinstance(val, list):
                        body["group_manage"][key] = []
            if "group_rules" in body["group_manage"]:
                val = body["group_manage"]["group_rules"]
                if isinstance(val, str):
                    try:
                        parsed = json.loads(val)
                        body["group_manage"]["group_rules"] = (
                            parsed if isinstance(parsed, list) else [])
                    except Exception:
                        body["group_manage"]["group_rules"] = []
                elif not isinstance(val, list):
                    body["group_manage"]["group_rules"] = []

        # 合并保存：只覆盖表单提交的键，避免把 Dashboard 未展示的配置
        # （如 judge_provider_id）在保存时被清空
        for section in ["reply_engine", "flow_engine", "debounce",
                        "accumulation", "proactive", "ai_timeout",
                        "group_manage"]:
            if section in body and isinstance(body[section], dict):
                existing = self.config.get(section, {}) or {}
                self.config[section] = {**existing, **body[section]}
                changed = True

        for key in ["interest_keywords", "reply_style", "ai_judge_prompt", "ai_reply_prompt"]:
            if key in body:
                self.config[key] = body[key]
                changed = True

        if changed:
            try:
                self.config.save_config()
            except Exception as e:
                return error_response(f"保存失败: {e}")

        # 同步内存中的模块配置
        self.flow._interest_keywords = self.config.get("interest_keywords", []) or []
        self.flow._cfg = self.config.get("flow_engine", {})
        self.flow._reply_cfg = self.config.get("reply_engine", {})
        self.debounce._dc = self.config.get("debounce", {})
        self.debounce._fc = self.config.get("flow_engine", {})
        self.accum._cfg = self.config.get("accumulation", {})
        self.accum._reply_cfg = self.config.get("reply_engine", {})
        self.ai._re_cfg = self.config.get("reply_engine", {})
        self.ai._cfg = self.config
        self.persona._inherit = self.config.get("reply_engine", {}).get(
            "inherit_persona", True)
        self.persona.invalidate()
        return json_response({"ok": True})

    async def _api_providers(self):
        """列出可用的对话模型提供商，供 Dashboard 的「AI判断专用模型」下拉框使用。"""
        try:
            provs = self.context.get_all_providers()
            result = []
            for p in provs or []:
                try:
                    meta = p.meta()
                    pid = meta.id
                    name = getattr(meta, "model_name", None) or pid
                except Exception:
                    continue
                if pid:
                    result.append({"id": pid, "name": name})
            return json_response(result)
        except Exception as e:
            return error_response(str(e))
