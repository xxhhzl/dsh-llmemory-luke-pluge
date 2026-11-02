import sys

import config
import base
import commands
import ingest
import llm
import routo
import error
import tools

def routo_init(routo_table: routo.routo_register):
    '''
    注册入站请求类型。
    注意：本项目不主动推送 tools.schema / commands，
    由 JS 外壳发同名请求帧索取，内核按同一 type 应答。
    '''
    routo_table.register("tools.schema", tools.get_tools_schema)
    routo_table.register("tools.calls", tools.tools_call)
    routo_table.register("commands", commands.get_commands)
    routo_table.register("config.set", config.config_set)
    routo_table.register("config.get", config.config_get)
    routo_table.register("health", base.health)
    routo_table.register("shutdown", base.shutdown)
    # 写入链路：立即回执，后台 worker 切分/摘要/落库
    routo_table.register("ingest.request", ingest.ingest_request)
    # 外壳代理的模型调用应答：按 request_id 唤醒等待中的 worker
    routo_table.register("llm.result", llm.on_result)


def drain(q: base.message_queue):
    '''
    同步输出泵（保留给测试与脚本）：把队列里积压的帧逐条按行写到 stdout。
    主循环不再调用它——异步帧由 base.start_writer 的常驻写线程输出。
    '''
    while True:
        out = q.recv()
        if out == "":
            break
        sys.stdout.write(out + "\n")
    sys.stdout.flush()


def main():
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")

    # 日志：stderr + store.db_path 同目录的 longmemory.log；stdout 只走协议帧
    base.setup_logging()

    # 预处理
    message_q: base.message_queue = base.message_queue()
    routo_table: routo.routo_register = routo.routo_register(message_q)

    # 模型调用一律走外壳代理：先绑定队列，后续业务模块用 llm.get_client()
    llm.bind(message_q)

    # 常驻写线程 + 后台 ingest worker：主线程只负责读 stdin 与分发，
    # 模型调用（llm.request/llm.result）和异步帧都不会阻塞它。
    writer_thread = base.start_writer(message_q)
    ingest.start_worker(message_q)

    ## 注册路由
    routo_init(routo_table)

    base.log.info("kernel.started", protocol=config.PROTOCOL_VERSION,
                  plugin=config.PLUGIN_VERSION,
                  routes=len(routo_table._registry))

    for line in sys.stdin:
        line = line.strip().lstrip("\ufeff")
        if not line:
            continue

        # 校验公共信封格式；坏帧回错误帧，不让进程退出
        ok, payload, field = config.check_json_format(line)
        if not ok:
            base.clear_log_context()
            if field == "type":
                # type 不在枚举内，按协议属「未知 type」
                error.send_error_message({}, message_q, -32601, "未知 type", payload)
            else:
                error.send_error_message({}, message_q, -32700, "JSON 解析失败", payload)
            continue

        data = payload
        base.set_log_context(session_id=data.get("session_id"),
                             agent_id=data.get("agent_id"),
                             request_id=data.get("request_id"),
                             job_id=data.get("job_id"))

        # 校验协议版本
        if not config.is_format(data["protocol_version"]):
            error.send_error_message(
                data, message_q, -32600, "协议版本不兼容",
                f"本插件 {config.PROTOCOL_VERSION}，收到 {data.get('protocol_version')}")
            continue

        # 分发：未注册的 type 回 -32601，处理器异常回 -32603
        try:
            handled = routo_table.routo(data)
        except Exception as e:
            error.send_error_message(data, message_q, -32603, "内核内部错误", repr(e))
            handled = True
        if not handled:
            error.send_error_message(data, message_q, -32601, "未知 type",
                                     f"未注册的 type: {data.get('type')}")

        # shutdown 已置位：等写线程排空队列、停掉它，再退出
        if base.shutdown_requested:
            drained = base.wait_drained(message_q, base.shutdown_drain_ms / 1000)
            base.stop_writer()
            writer_thread.join(timeout=1.0)
            base.log.info("kernel.stopped", drain_ms=base.shutdown_drain_ms,
                          drained=drained)
            base.clear_log_context()
            return

    # stdin 结束（外壳关闭）：同样先排空再退出，避免 daemon 写线程在解释器收尾时抢 stdout 锁
    base.wait_drained(message_q, 3.0)
    base.stop_writer()
    writer_thread.join(timeout=1.0)



if __name__ == "__main__":
    main()
