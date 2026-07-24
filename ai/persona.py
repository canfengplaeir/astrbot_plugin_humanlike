from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger


class PersonaBridge:
    """人格桥接：从 AstrBot 人格管理器读取当前会话的人格设定。"""

    def __init__(self, context, config):
        self._context = context
        self._inherit = config.get("reply_engine", {}).get("inherit_persona", True)

    @property
    def enabled(self) -> bool:
        return self._inherit

    async def system_prompt(self, event: AstrMessageEvent) -> str:
        if not self._inherit:
            return ""
        try:
            p = await self._context.persona_manager.get_default_persona_v3(
                event.unified_msg_origin
            )
            if p and p.get("prompt"):
                return str(p["prompt"]).strip()
        except Exception as e:
            logger.warning(f"获取人格设定失败: {e}")
        return ""

    async def name(self, event: AstrMessageEvent) -> str:
        if not self._inherit:
            return ""
        try:
            p = await self._context.persona_manager.get_default_persona_v3(
                event.unified_msg_origin
            )
            if p and p.get("name"):
                return str(p["name"]).strip()
        except Exception:
            pass
        return ""
