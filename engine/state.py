import asyncio
from dataclasses import dataclass, field

# 对话上下文保留条数（各路径统一使用）
MAX_CONTEXT = 12
# 消息时间戳最多保留条数，防止内存无限增长
MAX_MSG_TRACK = 200
# 消息时间戳保留的时间窗口（秒）：活跃度统计只看最近 60s，多保留一倍
MSG_TRACK_WINDOW = 120


@dataclass
class GroupState:
    flow_level: float = 50.0
    last_reply_time: float = 0.0
    reply_timestamps: list[float] = field(default_factory=list)
    last_update_time: float = 0.0
    conversation_context: list[dict] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_messages: list[dict] = field(default_factory=list)
    silence_timer: object = None
    last_msg_time: float = 0.0
    last_speaker_id: str = ""
    same_speaker_count: int = 0
    retry_count: int = 0
    # ---- v1.1 新增 ----
    # 群消息时间戳（用于“活跃度加成”的准确统计）
    msg_timestamps: list[float] = field(default_factory=list)
    # 该群会话的统一来源标识（platform:type:id），主动发言时发送消息用
    unified_msg_origin: str = ""
    # 批处理管道是否正在运行，防止并发触发导致重复回复
    pipeline_running: bool = False
    # 主动发言时间戳列表，用于冷却与每日上限统计
    proactive_timestamps: list[float] = field(default_factory=list)

    def record_msg_time(self, ts: float):
        """记录一条群消息时间，并裁剪过期数据。"""
        self.msg_timestamps.append(ts)
        if len(self.msg_timestamps) > MAX_MSG_TRACK or (
            self.msg_timestamps[0] < ts - MSG_TRACK_WINDOW
        ):
            cutoff = ts - MSG_TRACK_WINDOW
            self.msg_timestamps = [
                t for t in self.msg_timestamps if t >= cutoff
            ][-MAX_MSG_TRACK:]

    def append_context(self, sender: str, text: str):
        """向对话上下文追加一条并裁剪。"""
        self.conversation_context.append({"sender": sender, "text": text})
        if len(self.conversation_context) > MAX_CONTEXT:
            self.conversation_context = self.conversation_context[-MAX_CONTEXT:]
