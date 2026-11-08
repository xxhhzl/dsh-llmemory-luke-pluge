# -*- coding: utf-8 -*-
"""ANN 向量索引（usearch / HNSW）：vec0 是事实源，这里只是加速层。

为什么不直接读写 ANN 当存储：
- `vec0` 已经在仓库里（sqlite-vec），落库、过滤、删除都在 SQL 里可靠；
- ANN 索引是**进程内缓存**，进程重启后从 vec0 重新装载（90 条毫秒级；
  万级条目首次检索约 1s 量级），不做磁盘持久化，避免索引与库不同步；
- usearch 缺失（没装、加载失败、平台不支持）时整层跳过，
  `Database.search_vectors` 自动退回 `vec_search` 的精确 KNN，功能不变。

元数据过滤（项目范围）在 Python 侧做：先按 `k*OVERFETCH` 取候选再过滤，
不够就翻倍重取，直到够 k 条或取完整个索引——HNSW 的过滤召回问题在
「本项目 + 通用」这种小集合下用过量取数解决最稳。
"""
from __future__ import annotations

import threading
import weakref

import base

try:                                     # 缺依赖只降级，不影响主库
    import numpy as np
    from usearch.index import Index as _UsearchIndex
    _IMPORT_ERROR = ""
except Exception as exc:                 # pragma: no cover - 环境相关
    np = None
    _UsearchIndex = None
    _IMPORT_ERROR = repr(exc)

BACKEND = "usearch"
CONNECTIVITY = 16
EXPANSION_ADD = 128
EXPANSION_SEARCH = 64
OVERFETCH = 8                            # 先取 k*8 条再按项目过滤


def available() -> bool:
    return _UsearchIndex is not None and np is not None


def import_error() -> str:
    return _IMPORT_ERROR


class _KindIndex:
    '''单个 kind 的 HNSW 索引：ref_id(uint64) → 向量 + origin_project。'''

    def __init__(self, dim: int):
        self.dim = int(dim)
        self.index = _UsearchIndex(
            ndim=self.dim, metric="cos", dtype="f32",
            connectivity=CONNECTIVITY, expansion_add=EXPANSION_ADD,
            expansion_search=EXPANSION_SEARCH)
        self.projects: dict[int, str] = {}
        self.loaded = False

    def add(self, ref_id: int, vector, project: str | None) -> None:
        array = np.asarray(vector, dtype=np.float32).reshape(1, -1)
        if array.shape[1] != self.dim:
            raise ValueError(f"向量维度 {array.shape[1]} ≠ 索引 {self.dim}")
        self.index.add(np.asarray([int(ref_id)], dtype=np.uint64), array)
        self.projects[int(ref_id)] = project or ""

    def remove(self, ref_id: int) -> None:
        key = int(ref_id)
        if key in self.projects:
            try:
                self.index.remove(np.asarray([key], dtype=np.uint64))
            except Exception as exc:      # 已删除/不在索引里：只清元数据
                base.log.warning("ann.remove_failed", ref_id=key, error=repr(exc))
            self.projects.pop(key, None)

    def search(self, vector, k: int, allow: set | None = None) -> list:
        size = int(self.index.size)
        if size == 0:
            return []
        query = np.asarray(vector, dtype=np.float32).reshape(-1)
        take = min(size, max(k * OVERFETCH, k + 32))
        while True:
            result = self.index.search(query, take)
            pairs = [(int(key), float(distance))
                     for key, distance in zip(result.keys.tolist(),
                                              result.distances.tolist())
                     if int(key) >= 0]
            if allow is not None:
                pairs = [(ref_id, distance) for ref_id, distance in pairs
                         if self.projects.get(ref_id, "") in allow]
            if len(pairs) >= k or take >= size:
                return pairs[:k]
            take = min(size, take * 2)


class AnnStore:
    '''按 (数据库实例, kind) 懒加载/增量同步的索引集合。

    键必须是数据库实例：测试与脚本会同时打开多个库，按 kind 全局缓存会串库
    （A 库的向量出现在 B 库的检索结果里）。弱引用保证库关掉后索引自然回收。
    '''

    def __init__(self):
        self._lock = threading.RLock()
        self._indices: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()

    def _kinds(self, db) -> dict:
        indices = self._indices.get(db)
        if indices is None:
            indices = {}
            self._indices[db] = indices
        return indices

    def _sync(self, db, kind: str) -> _KindIndex | None:
        table_dim = getattr(db, "_vec_dim", 0)
        if not table_dim:
            return None
        indices = self._kinds(db)
        index = indices.get(kind)
        if index is None or index.dim != int(table_dim):
            index = _KindIndex(int(table_dim))
            indices[kind] = index

        if not index.loaded:
            # 首次加载：整表灌进来（进程内缓存，重启后重建，不落盘）
            for ref_id, project, blob in db.list_vec_rows(kind):
                if blob is not None:
                    index.add(ref_id, np.frombuffer(blob, dtype=np.float32),
                              project)
            index.loaded = True
            db.take_vec_changes(kind)      # 全量装载已包含这些变更
            return index

        for ref_id in db.take_vec_changes(kind):
            if ref_id in index.projects:   # 覆盖写：先摘掉旧向量再加新的
                index.remove(ref_id)
            row = db.get_vec_row(kind, ref_id)
            if row is not None:
                project, blob = row
                if blob is not None:
                    index.add(ref_id, np.frombuffer(blob, dtype=np.float32),
                              project)
        return index

    def search(self, db, kind: str, vector, *, k: int = 50,
               origin_project: str | None = None,
               scope: str = "all") -> dict | None:
        '''返回 {ref_id: distance}；不适用（无索引/维度不符）返回 None 交给精确路径。'''
        with self._lock:
            index = self._sync(db, kind)
            if index is None or int(index.index.size) == 0:
                return {}
            allow = None
            if scope == "project":
                allow = {origin_project or "", ""}
            pairs = index.search(vector, max(1, int(k)), allow=allow)
        return {ref_id: distance for ref_id, distance in pairs}

    def status(self, db=None) -> dict:
        with self._lock:
            indexed: dict = {}
            kinds = self._kinds(db) if db is not None else {}
            if db is None:                       # 不给库就把所有实例合起来看
                for instance in list(self._indices.values()):
                    for kind, index in instance.items():
                        indexed[kind] = indexed.get(kind, 0) \
                            + int(index.index.size)
            else:
                for kind, index in kinds.items():
                    indexed[kind] = int(index.index.size)
            return {"backend": BACKEND if available() else "",
                    "available": available(),
                    "indexed": indexed,
                    "error": _IMPORT_ERROR}


_store = AnnStore()


def get_store() -> AnnStore:
    return _store
