from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from contextvars import ContextVar
import logging
import os
import queue
import sqlite3
import sys
import threading
import time
from pathlib import Path

DEFAULT_SEND_TIMEOUT = 5.0   # 队列满时最长等待秒数
RETRY_INTERVAL = 0.5         # 队列满时的重试间隔秒数

def get_cur_time() -> str:
    '''
    获取当前东八区时间
    :return: 返回YYYY-MM-DOTHH:MM:SS+08:00字符串
    '''
    cur = datetime.now(tz=ZoneInfo("Asia/Shanghai"))
    return cur.isoformat(timespec="seconds")


def to_utc_iso(value: str | None, *, default: str | None = None) -> str:
    '''
    把 ISO 8601 时间归一成 UTC（后缀 +00:00），用于**跨时区**比较与排序。

    时间字符串只在同一时区下才能按字典序比较：外壳可能给 `Z`，模型可能给
    `+08:00`，混存后 `>=` 比较会整体偏移。归一后长度与格式一致，字典序 = 时间序。

    :param value: 待归一时间；空串/None 返回 default
    :param default: 解析失败时的兜底值（缺省返回当前东八区时间）
    '''
    text = str(value or "").strip()
    if not text:
        return default if default is not None else get_cur_time()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return default if default is not None else get_cur_time()
    if parsed.tzinfo is None:               # 无偏移按 UTC 解释
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


'''
消息队列：多生产者，单消费者，每个路由都持有引用。

背压策略（2026-11-03 定稿）：
- 队列有界（`store.queue_max`，默认 1000）；无限队列只会把「外壳不读 stdout」
  变成内存膨胀，日志里也看不出症状；
- 入队满时**等待有限时间**（默认 5s）后丢弃该帧并计数，绝不无限阻塞主线程
  （主线程卡住会连 stdin 都不读，整个内核失去响应）；
- 丢弃数与当前深度进 `health`，由外壳判断是否重启。
'''
class message_queue:
    _q: queue.Queue
    _check: object

    def __init__(self, max_size: int = 0):
        self._q = queue.Queue(max_size)
        self._dropped = 0
        self._cond = threading.Condition()
        self._closed = False

    def _put(self, message: str) -> bool:
        '''裸入队：不计数、不告警（重试路径不重复记账）。'''
        try:
            self._q.put_nowait(message)
        except queue.Full:
            return False
        with self._cond:
            self._cond.notify_all()
        return True

    def _note_drop(self, message: str) -> None:
        '''记录一次真正的丢弃（重试耗尽后的最终失败）。'''
        self._dropped += 1
        log.warning("queue.full", depth=self.depth(), dropped=self._dropped,
                    size=len(message))

    def send(self, message: str) -> bool:
        '''只尝试一次；失败即计一次丢弃。'''
        if self._put(message):
            return True
        self._note_drop(message)
        return False

    def send_blocking(self, message: str, timeout: float | None = None,
                      interval: float | None = None) -> bool:
        '''
        入队；队列满时每隔 interval 秒重试，累计等待不超过 timeout 秒。
        只投递一份，不会重复入队。
        :param timeout: 最长等待秒数，缺省取 DEFAULT_SEND_TIMEOUT；<=0 表示只尝试一次
        :param interval: 重试间隔秒数，缺省取 RETRY_INTERVAL
        :return: 入队成功返回 True，超时返回 False（由调用方降级处理）
        '''
        if timeout is None:
            timeout = DEFAULT_SEND_TIMEOUT
        if interval is None:
            interval = RETRY_INTERVAL
        if timeout <= 0:
            return self.send(message)

        deadline = time.monotonic() + timeout
        while True:
            if self._put(message):
                return True
            if time.monotonic() >= deadline:
                self._note_drop(message)      # 只在真正放弃时记一次
                return False
            time.sleep(interval)

    def recv(self) -> str:
        '''
        获取消息，队列为空则返回""
        '''
        try:
            res = self._q.get_nowait()
        except queue.Empty:
            return ""
        with self._cond:
            self._cond.notify_all()
        return res

    def recv_blocking(self, timeout: float | None = None) -> str:
        '''等待取一条消息；超时返回 ""（供常驻写线程使用）。'''
        try:
            res = self._q.get(timeout=timeout)
        except queue.Empty:
            return ""
        with self._cond:
            self._cond.notify_all()
        return res

    def task_done(self):
        '''
        必须在消费完成后调用此方法
        '''
        self._q.task_done()

    def depth(self) -> int:
        '''
        当前积压的待发帧数量；持续增大说明输出泵或外壳消费卡住。
        '''
        return self._q.qsize()

    @property
    def dropped(self) -> int:
        '''因队列满被丢弃的帧数（背压指标，进 health）。'''
        return self._dropped

    def wait_empty(self, timeout: float = 3.0) -> bool:
        '''等待队列清空；用条件变量唤醒，不做忙轮询。超时返回 False。'''
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while self._q.qsize() > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True


# 由 shutdown 处理函数置位，主循环据此收尾退出
shutdown_requested = False
shutdown_drain_ms = 0

# 常驻写线程：异步帧（ingest.completed 等）由它统一写 stdout，
# 主线程只读 stdin，不被模型调用或输出阻塞。
_writer_stop = threading.Event()


def start_writer(q: message_queue, stream=None,
                 interval: float = 0.1) -> threading.Thread:
    '''启动 stdout 写线程；进程退出时随之结束（daemon）。'''
    import sys

    out = stream or sys.stdout
    _writer_stop.clear()

    def loop():
        while not _writer_stop.is_set():
            item = q.recv_blocking(interval)
            if item:
                out.write(item + "\n")
                out.flush()

    thread = threading.Thread(target=loop, name="stdout-writer", daemon=True)
    thread.start()
    return thread


def stop_writer():
    _writer_stop.set()


def wait_drained(q: message_queue, timeout: float = 3.0) -> bool:
    '''等待写线程把队列排空；超时返回 False（条件变量唤醒，不忙轮询）。'''
    return q.wait_empty(timeout)


def _db_ok(cfg: dict) -> bool:
    '''探活用的存储检查：不创建任何文件，只读现有库或检查目录可写。'''
    db_path = ((cfg.get("store") or {}).get("db_path") or "").strip()
    if not db_path:
        return False

    path = Path(db_path)
    if path.exists():
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1) as conn:
                conn.execute("SELECT 1")
            return True
        except sqlite3.Error:
            return False

    return path.parent.exists() and os.access(path.parent, os.W_OK)


def health(data, q: message_queue):
    '''health：探活；外壳据此判断内核是否存活或需要重启。'''
    # 延迟导入：frame 反向依赖 base，顶层导入会成环
    from frame import push

    try:
        import config
        cfg = config.get_config()
        revision = config.get_revision()
    except Exception:
        cfg, revision = {}, 0

    push(q, data, "health", {
        "ok": True,
        "revision": revision,
        "queue_depth": q.depth(),
        "queue_dropped": getattr(q, "dropped", 0),
        "db_ok": _db_ok(cfg),
    })


def shutdown(data, q: message_queue):
    '''
    shutdown：校验 drain_ms 后置位退出标志，由主循环排空队列并退出。
    该帧不需要应答（协议里 shutdown 只有上行）。
    '''
    from error import send_error_message

    raw = (data.get("data") or {}).get("drain_ms", 3000)
    if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= 30000:
        send_error_message(data, q, -32602, "参数不合法",
                           "drain_ms 需要 0–30000 的整数")
        return

    global shutdown_requested, shutdown_drain_ms
    shutdown_requested = True
    shutdown_drain_ms = raw


# ======== 日志 ========
#
# 约定：
# - stdout 是协议通道（一帧一行 JSON），日志只走 stderr + 日志文件，绝不污染 stdout。
# - 默认人类可读单行：<ISO 时间> <级别> <logger> <事件> k=v ...
# - 不新增配置项：级别复用 config 的 log.level，日志文件放在 store.db_path 同目录。
# - 上下文（session_id / agent_id / request_id / job_id）由 set_log_context 设置，
#   之后同一执行流里的每条日志自动带上。

_LOG_NAME = "longmemory"
_LOG_FILE_NAME = "longmemory.log"
_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

# LogRecord 自带字段，不当作业务上下文输出
_RESERVED = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
})

_log_context: ContextVar[dict] = ContextVar("longmemory_log_context", default={})


class _TextFormatter(logging.Formatter):
    '''人类可读单行格式：时间 级别 logger 事件 k=v ...（异常另起行）。'''

    def format(self, record: logging.LogRecord) -> str:
        parts = [get_cur_time(), f"{record.levelname:<7}", record.name,
                 record.getMessage()]

        extras = []
        for key in sorted(record.__dict__):
            if key in _RESERVED or key.startswith("_"):
                continue
            value = record.__dict__[key]
            if value is None or value == "":
                continue
            extras.append(f"{key}={value}")
        if extras:
            parts.append(" ".join(extras))

        line = " ".join(parts)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class _ContextAdapter(logging.LoggerAdapter):
    '''把全局上下文、bind() 绑定字段、调用时的 k=v 全部归入 extra。'''

    _PASS_THROUGH = frozenset({"exc_info", "stack_info", "stacklevel", "extra"})

    def process(self, msg, kwargs):
        merged = dict(_log_context.get())
        merged.update(self.extra or {})

        # 允许 log.info("evt", key=value)：非标准参数一律收进 extra
        for key in list(kwargs):
            if key not in self._PASS_THROUGH:
                merged[key] = kwargs.pop(key)
        merged.update(kwargs.pop("extra", None) or {})

        kwargs["extra"] = merged
        kwargs.setdefault("stacklevel", 2)
        return msg, kwargs

    def bind(self, **fields) -> "_ContextAdapter":
        merged = dict(self.extra or {})
        merged.update(fields)
        return _ContextAdapter(self.logger, merged)


def get_logger(name: str = "") -> _ContextAdapter:
    '''取日志适配器；name 作为子 logger 后缀，如 get_logger("db") → longmemory.db。'''
    logger = logging.getLogger(f"{_LOG_NAME}.{name}" if name else _LOG_NAME)
    return _ContextAdapter(logger, {})


log = get_logger()


def set_log_context(**fields):
    '''
    设置当前执行流的日志上下文（session_id / agent_id / request_id / job_id 等）。
    传 None 的字段会被忽略；再次调用是覆盖而不是追加。
    '''
    _log_context.set({key: value for key, value in fields.items()
                      if value is not None})


def clear_log_context():
    '''清空日志上下文。'''
    _log_context.set({})


def _default_log_path() -> Path:
    '''日志文件默认放在 store.db_path 同目录，不新增配置项。'''
    import config  # 延迟导入：config 顶层依赖 base，不能反过来顶层导入

    db_path = (config.get_config().get("store") or {}).get(
        "db_path") or "memory.db"
    return Path(db_path).parent / _LOG_FILE_NAME


def setup_logging(level: str | None = None, path: str | Path | None = None,
                  force: bool = False) -> logging.Logger:
    '''
    初始化日志：stderr + 日志文件，两个 handler 同一个人类可读格式。

    :param level: 级别；缺省取 config 的 log.level（不新增配置项）
    :param path: 日志文件路径；缺省与 store.db_path 同目录
    :param force: 重新初始化（测试用），先移除并关闭旧 handler
    :return: 配置好的 logger
    '''
    logger = logging.getLogger(_LOG_NAME)
    if logger.handlers and not force:
        return logger

    if force:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()

    if level is None:
        import config
        level = (config.get_config().get("log") or {}).get("level", "info")
    logger.setLevel(_LEVELS.get(str(level).lower(), logging.INFO))
    logger.propagate = False

    formatter = _TextFormatter()

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    logger.addHandler(stderr_handler)

    target = Path(path) if path is not None else _default_log_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(target, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as exc:
        # 文件不可写不能阻断进程：降级为只写 stderr，并留下诊断
        logger.warning("log.file.unavailable", extra={
            "path": str(target), "error": str(exc)})

    return logger


def set_log_level(level: str) -> logging.Logger:
    '''运行期调整日志级别（config.set 改 log.level 后调用）。'''
    logger = logging.getLogger(_LOG_NAME)
    logger.setLevel(_LEVELS.get(str(level).lower(), logging.INFO))
    return logger

