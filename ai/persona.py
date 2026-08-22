import time

from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger

_DEFAULT_KEY = "__default__"


class PersonaBridge:
    """人格桥接：始终从 AstrBot 人格管理器继承当前会话的人格设定。

    插件完全为增强 AstrBot 自主回复而生：人格（含名字与语气）一律
    取自 AstrBot 当前选中的默认人格，插件不提供关闭或替代选项。

    带 TTL 缓存：旧版在每条群消息上都会调用两次
    get_default_persona_v3()，高活跃群聊中会产生大量重复的配置读取。
    """

    # 缓存有效期（秒）。人格在 WebUI 修改后最多延迟这么久生效。
    CACHE_TTL = 30.0

    def __init__(self, context, config):
        self._context = context
        # key -> (fetch_time, persona_dict_or_None)
        self._cache: dict[str, tuple[float, dict | None]] = {}

    @property
    def enabled(self) -> bool:
        """人格继承始终开启。"""
        return True

    def invalidate(self):
        """清空缓存（设置变更或人格被修改后调用）。"""
        self._cache.clear()

    async def _get(self, umo: str | None) -> dict | None:
        key = umo or _DEFAULT_KEY
        now = time.time()
        hit = self._cache.get(key)
        if hit and now - hit[0] < self.CACHE_TTL:
            return hit[1]
        try:
            p = await self._context.persona_manager.get_default_persona_v3(umo)
            persona = dict(p) if p else None
        except Exception as e:
            logger.warning(f"获取人格设定失败: {e}")
            persona = None
        self._cache[key] = (now, persona)
        return persona

    async def system_prompt(self, event: AstrMessageEvent) -> str:
        p = await self._get(getattr(event, "unified_msg_origin", None))
        if p and p.get("prompt"):
            return str(p["prompt"]).strip()
        return ""

    async def name(self, event: AstrMessageEvent) -> str:
        p = await self._get(getattr(event, "unified_msg_origin", None))
        if p and p.get("name"):
            return str(p["name"]).strip()
        return ""

    # ── 无事件场景（主动发言等）──────────────────────────────

    async def system_prompt_for(self, umo: str | None) -> str:
        p = await self._get(umo)
        if p and p.get("prompt"):
            return str(p["prompt"]).strip()
        return ""

    async def name_for(self, umo: str | None) -> str:
        p = await self._get(umo)
        if p and p.get("name"):
            return str(p["name"]).strip()
        return ""
