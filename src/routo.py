from typing import Callable
from base import message_queue

routo_func = Callable[..., None]

class routo_register:
    _registry: dict[str, routo_func]
    _q: message_queue

    def __init__(self, q: message_queue):
        self._registry = dict()
        self._q = q

    def register(self, type: str, func: routo_func):
        '''
        注册路由
        存在则覆盖
        '''
        self._registry[type] = func

    def logoff(self, type: str):
        '''
        注销luy
        不存在则不抛异常
        '''
        self._registry.pop(type, None)

    def routo(self, data) -> bool:
        '''
        路由
        :return: 已注册并执行返回 True；未注册的 type 返回 False；
                 处理器抛出的异常不在此吞掉，交给调用方转成 -32603
        '''
        func = self._registry.get(data.get("type"))
        if func is None:
            return False

        func(data, self._q)
        return True
