import asyncio
from dataclasses import dataclass, field


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
