"""指令定义与下发。
指令是**声明式**的：内核只描述外壳该渲染什么，不执行指令本身。
用户执行 `/memory` 时由外壳发 `ingest.request`。
"""
import re

import base
import frame

# 允许的指令名：小写字母开头，只含小写字母/数字/下划线/连字符，长度 1–32
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_CATEGORIES = ("context", "memory", "debug")
_ARG_TYPES = ("boolean", "string", "integer", "enum")

# 已注册的指令：name -> 定义字典
commands = dict()


def _check_arguments(arguments):
    '''校验参数定义，失败抛 ValueError（导入期即暴露问题）。'''
    seen = set()
    result = []
    for arg in arguments:
        name = arg.get("name")
        if not name or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name):
            raise ValueError(f"指令参数名不合法: {name!r}")
        if name in seen:
            raise ValueError(f"指令参数名重复: {name}")
        seen.add(name)

        arg_type = arg.get("type")
        if arg_type not in _ARG_TYPES:
            raise ValueError(f"参数 {name} 的 type 不合法: {arg_type!r}")
        if arg_type == "enum" and not arg.get("choices"):
            raise ValueError(f"参数 {name} 是 enum 但缺少 choices")

        spec = {
            "name": name,
            "flag": arg.get("flag") or f"--{name.replace('_', '-')}",
            "type": arg_type,
            "required": bool(arg.get("required", False)),
            "default": arg.get("default"),
            "description": arg.get("description", ""),
        }
        if arg_type == "enum":
            spec["choices"] = list(arg["choices"])
        result.append(spec)
    return result


def command(name, description, aliases=None, usage=None, arguments=None,
            requires_confirmation=False, category="context"):
    '''
    注册一条指令定义。

    :param name: 指令名，匹配 ^[a-z][a-z0-9_-]{0,31}$
    :param description: 一行说明，显示在指令面板
    :param aliases: 别名列表，同样受命名约束且不得与其他指令重名
    :param usage: 用法提示；缺省由 arguments 推导
    :param arguments: 参数定义列表，见 docs/Transmission.md §3.2
    :param requires_confirmation: 执行前是否弹确认框
    :param category: 面板分组，context / memory / debug
    :return: 该指令的定义字典
    '''
    if not _NAME_RE.fullmatch(name or ""):
        raise ValueError(f"指令名不合法: {name!r}")
    if name in commands:
        raise ValueError(f"指令重复注册: {name}")
    if category not in _CATEGORIES:
        raise ValueError(f"指令分组不合法: {category!r}")

    alias_list = list(aliases or [])
    for alias in alias_list:
        if not _NAME_RE.fullmatch(alias):
            raise ValueError(f"别名不合法: {alias!r}")
        if alias == name or alias in commands:
            raise ValueError(f"别名与已有指令冲突: {alias}")
        if any(alias in spec["aliases"] for spec in commands.values()):
            raise ValueError(f"别名重复: {alias}")

    arg_list = _check_arguments(list(arguments or []))

    if usage is None:
        flags = " ".join(f"[{a['flag']}]" for a in arg_list)
        usage = f"/{name} {flags}".strip()

    spec = {
        "name": name,
        "aliases": alias_list,
        "description": description,
        "usage": usage,
        "arguments": arg_list,
        "requires_confirmation": bool(requires_confirmation),
        "category": category,
    }
    commands[name] = spec
    return spec


def get_commands(data, q: base.message_queue):
    '''
    应答外壳的 commands 拉取请求：下发全部已注册指令定义。
    '''
    frame.push(q, data, "commands",
               [spec for spec in commands.values()],
               detail="消息队列已满，commands 未能下发")


# ---- 指令定义 ----

command(
    name="memory",
    aliases=["mem"],
    description="把当前会话写入长期记忆并切分事件",
    arguments=[
        {
            "name": "full",
            "flag": "--full",
            "type": "boolean",
            "required": False,
            "default": False,
            "description": "忽略游标重扫整个会话",
        },
        {
            "name": "mark_end",
            "flag": "--mark-end",
            "type": "boolean",
            "required": False,
            "default": False,
            "description": "把最后一条 assistant 消息视为任务终点",
        },
    ],
    category="context",
)
