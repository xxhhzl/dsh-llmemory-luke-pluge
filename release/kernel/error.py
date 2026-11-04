import base
import config
import sys

class FormatException(Exception):
    '''
    格式错误
    '''
    ...

def send_error_message(envelope:dict, q: base.message_queue, code: int, message: str,
                       detail: str, retry_after_ms = None, wait: bool = True) -> bool:
    '''
    组装并投递错误帧。
    :param wait: True 表示队列满时按 base.RETRY_INTERVAL 重试、最多等 base.DEFAULT_SEND_TIMEOUT；False 表示只尝试一次
    :return: 投递成功返回 True
    '''
    payload = config.build_frame(
        "error",
        {"code": code, "message": message,
         "detail": detail, "retry_after_ms": retry_after_ms},
        envelope,
    )
    ok = q.send_blocking(payload) if wait else q.send(payload)
    if not ok:
        print(f"[longmemory] 错误帧投递失败（队列已满）: {code} {message}", file=sys.stderr)
    return ok