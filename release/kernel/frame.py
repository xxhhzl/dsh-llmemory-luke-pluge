# -*- coding: utf-8 -*-
"""下行帧的统一投递。

三个 handler（tools.schema / commands / 后续的 library.data 等）都要做同样三件事：
构造信封 → 投递 → 队列满时降级为错误帧。这里只做这一件事，不掺业务。

依赖方向：frame → config / error / base，三者互不反向依赖本模块，故无循环导入。
"""
import base
import config
from error import send_error_message

CODE_INTERNAL = -32603


def push(q: base.message_queue, envelope: dict, type: str, data: dict | list,
         detail: str = "") -> bool:
    '''
    构造一帧并投递。

    :param q: 消息队列（由 main 的输出泵消费）
    :param envelope: 入站信封，用于继承 request_id/job_id/session_id/agent_id
    :param type: 帧类型，取值见 docs/Transmission.md §2
    :param data: 该类型的负载
    :param detail: 队列满时写入错误帧的补充说明；缺省为「<type> 未能下发」
    :return: 投递成功返回 True；构造失败或队列满返回 False（错误帧已尽量投递）
    '''
    try:
        payload = config.build_frame(type, data, envelope)
    except Exception as e:
        send_error_message(envelope, q, CODE_INTERNAL, "内核内部错误", repr(e))
        return False

    if q.send_blocking(payload):
        return True

    # 队列持续满：降级为错误帧，只尝试一次，不叠加一轮等待
    send_error_message(envelope, q, CODE_INTERNAL, "内核内部错误",
                       detail or f"消息队列已满，{type} 未能下发",
                       1000, wait=False)
    return False
