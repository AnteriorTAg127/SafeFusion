"""存储层：SQLite DAO（Database）与自研 numpy 向量库（NumpyVectorStore）。

对外导出（供 engines / cache / core / api 各层使用）：
- ``Database``：审核记录 / API Key / 词库 / 白名单元数据四表 DAO；
- ``BaseVectorStore`` / ``NumpyVectorStore``：向量库抽象与默认 numpy 后端；
- ``VectorItem`` / ``SearchHit``：统一接口契约的入库条目与检索命中结构。
"""

from .database import Database
from .vector_store import BaseVectorStore, NumpyVectorStore, SearchHit, VectorItem

__all__ = [
    "BaseVectorStore",
    "Database",
    "NumpyVectorStore",
    "SearchHit",
    "VectorItem",
]
