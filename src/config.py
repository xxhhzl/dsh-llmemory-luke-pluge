from typing import Literal
from pydantic import  BaseModel, Field, ValidationError

import copy
import os
import time

import base

PROTOCOL_VERSION = "1.0" # 协议版本
PLUGIN_VERSION = "0.3.2" # 插件版本

# 检查协议版本
def is_format(protocol_version: str) -> bool:
    '''
    检查协议版本（只比主版本，次版本差异按兼容处理）
    :param protocol_version: 协议版本
    :return: 兼容返回 True，不兼容返回 False
    '''
    return PROTOCOL_VERSION.split(".")[0] == protocol_version.split(".")[0]

# 校验json格式
FrameType = Literal[
    "tools.schema", "commands",
    "tools.calls", "tools.result",
    "ingest.request", "ingest.receipt", "ingest.completed",
    "library.query", "library.data", "notify",
    "llm.request", "llm.result",
    "embed.request", "embed.result",
    "error", "config.set", "config.get", "health", "shutdown", "stats", "admin",
    "core.get", "core", "settings.raw",
]

class Link_Json(BaseModel):
    protocol_version: str = Field(description="协议版本")
    plugin_version: str = Field(description="插件版本")
    type: FrameType = Field(description="请求类型，取值见 docs/Transmission.md §2")
    request_id: str = Field(description="请求id")
    job_id: str | None  = Field(description="异步任务标识，回执与完成通知共用同一个值", default=None)
    session_id: str = Field(description="会话标识，决定数据落在哪个命名空间")
    agent_id: str = Field(description="agent的名称")
    ts: str = Field(description="时间")
    data: dict | list

def check_json_format(data: str):
    '''
    判断json公共信封格式
    :param data: json字符串
    :return: 成功 (True, 信封字典, "")；
             失败 (False, 错误说明, 出错字段名)。字段名为 "" 表示整体 JSON 非法，
             为 "type" 表示 type 不在枚举内（调用方据此回 -32601 而不是 -32700）
    '''
    try:
        # 直接校验json字符串
        res = Link_Json.model_validate_json(data)
        return True, res.model_dump(), ""
    except ValidationError as e:
        # 只取首条错误的定位与说明，避免把 pydantic 的长报告（含官网链接）塞进 detail
        err = e.errors()[0]
        loc = err.get("loc", ())
        field = next((str(part) for part in loc
                      if isinstance(part, str) and part != "body"), "")
        msg = err.get("msg", "未知校验错误")
        return False, f"{field}: {msg}" if field else msg, field


# 构造下行帧：统一信封来源，避免各处手搓导致字段漂移
def build_frame(type: str, data: dict | list,
                envelope: dict | None = None) -> str:
    '''
    构造并序列化一帧（单行 JSON，中文原样输出，不转义）。
    :param type: 帧类型，取值见 docs/Transmission.md §2
    :param data: 该类型的负载
    :param envelope: 入站信封，用于继承 request_id/job_id/session_id/agent_id；
                     缺省时这些字段填空串（仅用于无来源可继承的场景）
    :return: 不含换行的 JSON 字符串
    '''
    env = envelope or {}
    link = Link_Json(
        protocol_version=PROTOCOL_VERSION,
        plugin_version=PLUGIN_VERSION,
        type=type,
        request_id=env.get("request_id", ""),
        job_id=env.get("job_id"),
        session_id=env.get("session_id", ""),
        agent_id=env.get("agent_id", ""),
        ts=base.get_cur_time(),
        data=data,
    )
    return link.model_dump_json(ensure_ascii=False)


# ======== 运行期配置（内核侧参数；模型侧 provider/model/key 归外壳） ========

DEFAULT_CONFIG = {
    "jev": {
        "on_uncertain": "escalate",    # continue / split / escalate
    },
    "split": {
        "gap_minutes": 120,            # 软先验：间隔超过该值交模型判（不再直接切）
        "max_event_tokens": 128000,    # 主限制：单事件正文的 token 预算
        "max_messages": 1000,          # 紧急兜底：只在异常长事件时触发
        "token_scope": "dialog",       # token 计数口径：dialog 只数 user/assistant；all 连工具输出一起数
    },
    "search": {
        "limit_max": 20,               # memory_search 的 limit 上限（运行时 clamp）
        "min_score": 0.0,              # 默认分数下限
        "default_scope": "all",        # project / all；外壳开始透传 origin_project 后可切 project
        "alpha": 0.6,                  # 精排：相关性占比（其余为记忆强度）
        "w_activation": 0.45,          # 记忆强度：ACT-R 激活
        "w_importance": 0.25,          # 记忆强度：重要性
        "w_confidence": 0.15,          # 记忆强度：确信度/可靠性
        "w_project": 0.10,             # 记忆强度：情境匹配
        "explore_ratio": 0.1,          # 探索位比例（冷条目曝光）
        "max_per_cluster": 2,          # 同一事件簇最多几条
        "cross_project_penalty": 0.0,  # 跨项目召回的额外衰减 0–1（0=关闭，A/B 后再调）
    },
    "store": {
        "db_path": ".dsh-longmemory/memory.db",
        "busy_timeout_ms": 5000,
        "queue_max": 1000,
    },
    "core": {
        "enabled": True,               # 是否编译并注入核心记忆
        "max_chars": 2000,             # 系统提示词核心块总预算（字符）
        "max_insights": 6,             # 入选核心块的洞见上限
        "max_experiences": 4,          # 入选核心块的全局经验上限
        "max_cognitions": 4,           # 入选核心块的全局认知上限
        "max_self_configured": 20,     # 设置的自我（进系统提示词）上限
        "max_self_notes": 10,          # 自己写的自我（进动态上下文）上限
        "section_order": -2000,       # 系统提示词段顺序（最前，先于 harness 身份 -1000）
        "context_order": 850,          # 动态上下文顺序
    },
    "embedding": {
        "dim": 1024,                   # bge-m3 维度；换模型需重建向量表
    },
    "log": {"level": "info"},           # debug / info / warn / error
}

# 叶子级校验规则；未列出的路径一律视为未知键（拒绝整个补丁）
_LEAF_SPEC = {
    "jev.on_uncertain": {"type": str, "enum": {"continue", "split", "escalate"}},
    "split.gap_minutes": {"type": int, "min": 0, "max": 10080},
    "split.max_event_tokens": {"type": int, "min": 500, "max": 200000},
    "split.max_messages": {"type": int, "min": 1, "max": 5000},
    "split.token_scope": {"type": str, "enum": {"dialog", "all"}},
    "search.limit_max": {"type": int, "min": 1, "max": 100},
    "search.min_score": {"type": float, "min": 0.0, "max": 1.0},
    "search.default_scope": {"type": str, "enum": {"project", "all"}},
    "search.alpha": {"type": float, "min": 0.0, "max": 1.0},
    "search.w_activation": {"type": float, "min": 0.0, "max": 1.0},
    "search.w_importance": {"type": float, "min": 0.0, "max": 1.0},
    "search.w_confidence": {"type": float, "min": 0.0, "max": 1.0},
    "search.w_project": {"type": float, "min": 0.0, "max": 1.0},
    "search.explore_ratio": {"type": float, "min": 0.0, "max": 1.0},
    "search.max_per_cluster": {"type": int, "min": 1, "max": 20},
    "search.cross_project_penalty": {"type": float, "min": 0.0, "max": 1.0},
    "store.db_path": {"type": str, "min_len": 1},
    "store.busy_timeout_ms": {"type": int, "min": 0, "max": 600000},
    "store.queue_max": {"type": int, "min": 1, "max": 1000000},
    "core.enabled": {"type": bool},
    "core.max_chars": {"type": int, "min": 200, "max": 20000},
    "core.max_insights": {"type": int, "min": 0, "max": 50},
    "core.max_experiences": {"type": int, "min": 0, "max": 50},
    "core.max_cognitions": {"type": int, "min": 0, "max": 50},
    "core.max_self_configured": {"type": int, "min": 0, "max": 100},
    "core.max_self_notes": {"type": int, "min": 0, "max": 100},
    "core.section_order": {"type": int, "min": -10000, "max": 20000},
    "core.context_order": {"type": int, "min": -10000, "max": 20000},
    "embedding.dim": {"type": int, "min": 64, "max": 8192},
    "log.level": {"type": str, "enum": {"debug", "info", "warn", "error"}},
}

# 外壳通过环境变量指定库路径：内核进程的 cwd 与日志目录都不再依赖相对路径
if os.environ.get("DSH_LONGMEMORY_DB"):
    DEFAULT_CONFIG["store"]["db_path"] = os.environ["DSH_LONGMEMORY_DB"]

_config = copy.deepcopy(DEFAULT_CONFIG)
_revision = 1
# 由检索/存储层注入：callable() -> None；抛异常表示重建失败
_reload_hook = None


def _flatten(patch, prefix=""):
    '''把嵌套补丁摊平成 (路径, 叶子值)。对象必须写到叶子，不允许空对象。'''
    for key, value in patch.items():
        if not isinstance(key, str):
            raise ValueError(f"配置键必须是字符串: {key!r}")
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            if not value:
                raise ValueError(f"{path} 是空对象，补丁必须写到叶子")
            yield from _flatten(value, f"{path}.")
        else:
            yield path, value


def _check_leaf(path: str, value):
    '''校验单个叶子；失败抛 ValueError（调用方转 -32602）。'''
    spec = _LEAF_SPEC.get(path)
    if spec is None:
        raise ValueError(f"未知配置项: {path}")

    want = spec["type"]
    if want is bool:
        if not isinstance(value, bool):
            raise ValueError(f"{path} 需要布尔值")
    elif want is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{path} 需要整数")
    elif want is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{path} 需要数字")
    elif want is str:
        if not isinstance(value, str):
            raise ValueError(f"{path} 需要字符串")

    if "min" in spec and value < spec["min"]:
        raise ValueError(f"{path} 不得小于 {spec['min']}")
    if "max" in spec and value > spec["max"]:
        raise ValueError(f"{path} 不得大于 {spec['max']}")
    if "enum" in spec and value not in spec["enum"]:
        raise ValueError(f"{path} 只能是 {sorted(spec['enum'])} 之一")
    if "min_len" in spec and len(value) < spec["min_len"]:
        raise ValueError(f"{path} 长度至少 {spec['min_len']}")


def _set_path(tree: dict, path: str, value):
    node = tree
    parts = path.split(".")
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value


def get_config() -> dict:
    '''返回当前生效配置的深拷贝。'''
    return copy.deepcopy(_config)


def get_revision() -> int:
    '''配置版本号；每次成功 config.set 递增。'''
    return _revision


def set_reload_hook(hook):
    '''注册 reload 钩子（重建检索索引）；传 None 表示未接入。'''
    global _reload_hook
    _reload_hook = hook


def patch_config(patch, reload: bool = False) -> dict:
    '''
    应用配置补丁：先全量校验，全部通过才替换；任一叶子不合法则整体拒绝。

    :param patch: 嵌套字典，只写要改的叶子
    :param reload: True 时先跑 reload 钩子（重建检索索引），失败则配置不生效
    :return: {"applied": [...], "revision": n, "reload_ms": int|None}
    '''
    global _config, _revision

    if not isinstance(patch, dict):
        raise ValueError("patch 必须是对象")

    leaves = list(_flatten(patch))
    if not leaves:
        raise ValueError("patch 不能为空")

    candidate = copy.deepcopy(_config)
    for path, value in leaves:
        _check_leaf(path, value)
        _set_path(candidate, path, value)

    reload_ms = None
    if reload:
        started = time.monotonic()
        if _reload_hook is not None:
            _reload_hook()          # 抛异常 → 调用方回 -32603，旧配置与旧索引保持不动
        reload_ms = int((time.monotonic() - started) * 1000)

    _config = candidate
    _revision += 1

    # 日志级别即时生效（不新增配置项，复用 log.level）
    if any(path == "log.level" for path, _ in leaves):
        base.set_log_level(_config["log"]["level"])

    return {"applied": [path for path, _ in leaves],
            "revision": _revision,
            "reload_ms": reload_ms}


def config_set(data, q: base.message_queue):
    '''config.set：热更新内核侧配置。'''
    # 延迟导入：error / frame 反向依赖本模块，顶层导入会成环
    from error import send_error_message
    from frame import push

    payload = data.get("data") or {}
    try:
        result = patch_config(payload.get("patch"),
                              bool(payload.get("reload", False)))
    except ValueError as exc:
        send_error_message(data, q, -32602, "参数不合法", str(exc))
        return
    except Exception as exc:
        send_error_message(data, q, -32603, "内核内部错误", f"reload 失败: {exc}")
        return

    push(q, data, "config.set", result)


def config_get(data, q: base.message_queue):
    '''config.get：返回当前生效配置与版本号。'''
    from frame import push

    push(q, data, "config.get",
         {"config": get_config(), "revision": get_revision()})
