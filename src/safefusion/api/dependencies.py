"""管理 API 公共依赖：X-Admin-Token 令牌鉴权与分页参数（PRD §4.2）。

供 ``api.admin`` 使用，未来审核 API（``api.app``）如采用同款分页约定也可复用。

v0.3.0（M4 热应用）：管理令牌改为 :class:`AdminToken` 可变容器承载——修改
管理密码后经 ``AdminToken.set()`` 立即热切换（旧令牌随即失效），鉴权依赖在
每次请求时读取容器当前值，不再在创建时冻结。
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Header, HTTPException, Query

#: 分页单页上限：防止管理员误传超大 page_size 拖垮 SQLite 全表扫描
_MAX_PAGE_SIZE = 500


class AdminToken:
    """可变管理令牌容器（热应用：修改密码后立即生效，旧令牌即刻失效）。

    用法：``store = AdminToken(初值)`` → ``require_admin_token(store)`` 注册
    依赖（每次请求读 ``store.value``）；修改密码时调用 ``store.set(new)``，
    无需重建应用或重启：新令牌立即放行、旧令牌立即 401。
    """

    def __init__(self, token: str) -> None:
        self._token = token

    @property
    def value(self) -> str:
        """当前生效的管理令牌。"""

        return self._token

    def set(self, token: str) -> None:
        """热切换管理令牌（旧令牌立即失效）。"""

        self._token = token


@dataclass(frozen=True)
class Page:
    """分页结果：换算好的 offset/limit 与原始 page/page_size。"""

    page: int
    page_size: int
    offset: int
    limit: int


def pagination(
    page: Annotated[int, Query(ge=1, description="页码，从 1 开始")] = 1,
    page_size: Annotated[int, Query(ge=1, le=_MAX_PAGE_SIZE, description="每页条数")] = 20,
) -> Page:
    """FastAPI 依赖：解析 ``page`` / ``page_size`` 并换算偏移量。

    用法：``page: Annotated[Page, Depends(pagination)]``。
    """

    return Page(
        page=page,
        page_size=page_size,
        offset=(page - 1) * page_size,
        limit=page_size,
    )


def require_admin_token(expected: str | AdminToken) -> Callable[..., str]:
    """构造「请求头 ``X-Admin-Token`` == 当前管理令牌」的 FastAPI 依赖工厂。

    Args:
        expected: 期望的管理令牌；可为字符串（创建时冻结，兼容 v0.2.1）或
            :class:`AdminToken` 容器（每次请求读取当前值，支持热切换）。

    Returns:
        依赖函数；令牌缺失或不匹配时抛 ``HTTPException(401)``。
        比较使用 ``hmac.compare_digest``（常数时间，防时序侧信道）。
    """

    def _current() -> str:
        return expected.value if isinstance(expected, AdminToken) else expected

    def _dependency(
        token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
    ) -> str:
        if token is None or not hmac.compare_digest(token, _current()):
            raise HTTPException(
                status_code=401,
                detail="认证失败：X-Admin-Token 请求头缺失或不正确",
            )
        return token

    return _dependency
