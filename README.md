# 拟人化群聊助手 (astrbot_plugin_humanlike)

让 AstrBot 在群聊中像真人一样说话——不再每条消息都回复，而是基于心流引擎、消息累积和 AI 判断，自主决定何时参与对话。

## 核心问题

默认 AstrBot 对群聊**每条消息**都回复，像话痨。本插件解决三个问题：

| 问题 | 方案 |
|------|------|
| 每条消息都回复 | 心流引擎 + 累积模式 → 有选择地参与 |
| 短时间刷屏 | 防抖 → 动态冷却 + 频率限制 + 随机沉默 |
| 回复缺乏人格 | 继承 AstrBot 人格系统 → 猫娘/傲娇等人设完整保留 |

## 架构概览

```
群聊消息
    │
    ▼
┌──────────────────────┐
│  心流引擎 (Flow)      │  更新参与意愿 (0-100)
│  @提及+35/关键词+15   │  每秒自然衰减 0.4
└──────────┬───────────┘
           │
           ▼
     ┌─ 立即触发？ ─────────────────────┐
     │  @提及 / 被叫名字 / 心流≥85      │
     │                                  │
     ▼ YES                              ▼ NO
┌──────────────┐              ┌──────────────────┐
│  立即处理     │              │  累积缓冲         │
│  检查防抖     │              │  加入 pending     │
│  AI 判断     │              │  重置 8s 计时器   │
│  生成回复     │              │  stop_event()    │
└──────────────┘              └────────┬─────────┘
                                       │
                                 8s 无新消息
                                 或缓冲 ≥ 20条
                                       │
                                       ▼
                              ┌──────────────────┐
                              │  批量处理          │
                              │  检查防抖          │
                              │  AI 综合判断       │
                              │  生成回复          │
                              └──────────────────┘
```

## 四大子系统

### 1. 心流引擎 (Flow Engine)

模拟人类注意力波动，每个群独立维护 0-100 的心流值。

**上升机制：**

| 触发条件 | 加成 | 说明 |
|----------|------|------|
| 被 @提及 | +35 | 直接叫到机器人 |
| 名字被提到 | +35 | 人格名或 bot_name 出现在消息中 |
| 匹配关键词 | +15 | 配置的兴趣话题 |
| 消息含问号 | +10 | 可能有人在提问 |
| 群聊活跃 | +3 | 60秒内多人发言 |

**下降机制：**

| 触发条件 | 衰减量 |
|----------|--------|
| 自然流失 | 每秒 -0.4 |
| 发言后衰减 | -30 |

心流值达到回复阈值（默认 45）才考虑回复。调低 → 话痨，调高 → 高冷。

### 2. 消息累积 (Accumulation)

模拟人看手机的习惯——消息轰炸时先不回复，安静下来再一起看。

**两条路径：**

| 路径 | 触发条件 | 行为 |
|------|----------|------|
| 立即处理 | 被@、被叫名字、心流≥85 | 跳过等待，直接判断回复 |
| 累积等待 | 普通聊天消息 | 加入缓冲队列，8秒无新消息后批量处理 |

**批量处理时**，多条消息合并为一个上下文发送给 LLM，由 AI 综合判断是否发言。这避免了针对每条消息单独判断的碎片化问题。

**保障机制：**
- 缓冲达到 20 条自动强制处理（防内存增长）
- `accumulation.enabled = false` 恢复逐条处理旧行为

### 3. 消息防抖 (Debounce)

三层保护避免刷屏：

```
第一层：动态冷却
  心流=100 → 冷却 10s（兴致高可以多说）
  心流=50  → 冷却 35s
  心流=0   → 冷却 60s（没兴趣少说）

第二层：频率限制
  默认每 5 分钟最多 6 次

第三层：随机沉默
  默认 15% 概率保持沉默（模拟偶尔不想说话）
```

### 4. AI 判断与回复

两级 LLM 调用：

**Prompt 结构（继承人格时）：**

```
【核心人格设定（必须严格遵守）】     ← PersonaManager 读取
{猫娘人设：你是xxx，性格xxx...}

【行为指令】                       ← 插件叠加
{自然回复、简洁、不要机械感}

语气微调：随性自然                  ← 在人设上的微调
心流值：75/100

最近群聊：...
```

- 人格来自 AstrBot WebUI 配置，插件不覆盖
- `reply_style` 定位为「语气微调」非「人格替换」
- AI 调用失败自动回退启发式规则

## 代码结构

模块化设计，便于维护和测试：

```
astrbot_plugin_humanlike/
├── main.py              # 编排层：Star 子类 + 指令 + 流程调度
├── metadata.yaml
├── _conf_schema.json
├── engine/              # 纯逻辑层，无 I/O 依赖
│   ├── state.py         # GroupState 数据类
│   ├── flow.py          # FlowEngine  — 心流更新
│   ├── debounce.py      # DebounceChecker — 防抖判断
│   └── accumulator.py   # AccumulationManager — 累积 + 计时器
└── ai/                  # AI 交互层
    ├── persona.py       # PersonaBridge — 人格桥接
    └── client.py        # AIClient — LLM 调用封装 (判断/回复/批量)
```

**依赖关系（无循环依赖）：**

```
main.py
  ├── FlowEngine          (纯计算，无 I/O)
  ├── DebounceChecker     (纯计算，无 I/O)
  ├── AccumulationManager (含 asyncio 计时器)
  ├── PersonaBridge       (async → persona_manager)
  └── AIClient            (async → llm_generate)
```

## 安装与使用

### 安装

1. 将 `astrbot_plugin_humanlike` 目录放入 `AstrBot/data/plugins/`
2. 重启 AstrBot 或在 WebUI 启用插件
3. 插件自动读取 AstrBot 的 AI 提供商和人格设定

### 指令

| 指令 | 说明 |
|------|------|
| `/mindflow` | 查看状态：心流值、上次发言、窗口内发言数、累积缓冲 |
| `/mindflowreset` | 重置当前群聊状态 |

```
/mindflow

🤖 状态
心流 [████████████░░░░░░░░] 62/100
上次: 3min | 窗口: 2/6
缓冲: 5条
```

### 关键调参

| 场景 | 调整 |
|------|------|
| 机器人太沉默 | 降低 `flow_reply_threshold`（如 30）和 `silence_threshold`（如 5） |
| 机器人太话痨 | 提高 `flow_reply_threshold`（如 60）和 `min_reply_cooldown`（如 45） |
| 想要立即响应 | 降低 `immediate_flow_threshold`（如 60） |
| 想要读消息延迟 | 提高 `silence_threshold`（如 15） |
| 关闭累积模式 | `accumulation.enabled = false` |

## 完整配置参考

```json
{
  "reply_engine": {
    "override_group_replies": true,
    "allow_command_pass": true,
    "inherit_persona": true,
    "enable_ai_judge": true,
    "bot_name": "",
    "record_conversation": true
  },
  "flow_engine": {
    "flow_decay_rate": 0.4,
    "initial_flow": 50,
    "flow_reply_threshold": 45,
    "flow_boost_mention": 35,
    "flow_boost_keyword": 15,
    "flow_boost_question": 10,
    "flow_boost_activity": 3,
    "flow_decay_on_reply": 30,
    "random_silence_probability": 15
  },
  "accumulation": {
    "enabled": true,
    "silence_threshold": 8,
    "immediate_flow_threshold": 85,
    "max_buffer_size": 20
  },
  "debounce": {
    "min_reply_cooldown": 25,
    "max_replies_per_window": 6,
    "reply_window_seconds": 300,
    "dynamic_cooldown_enabled": true,
    "min_dynamic_cooldown": 10,
    "max_dynamic_cooldown": 60
  },
  "interest_keywords": [],
  "reply_style": "随性自然"
}
```

## 设计原则

1. **人格不覆盖**：继承 AstrBot PersonaManager，用户人设完整保留。`reply_style` 仅为语气微调。
2. **累积优先**：消息爆发时缓冲，安静后批量处理，减少碎片化判断，模拟人类读消息行为。
3. **防抖优先于 AI**：防抖检查在 LLM 调用之前，避免不必要的 API 开销和延迟。
4. **错误容忍**：AI 调用失败自动回退启发式规则，插件不崩溃。
5. **群间隔离**：心流、上下文、计时器、缓冲全部按群独立。
6. **对话历史同步**：生成的回复写回 AstrBot ConversationManager，保持记录完整。
