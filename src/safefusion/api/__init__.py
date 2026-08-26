"""管理 API 包（:8001，PRD §4.2）。

- ``api.admin``：``create_admin_app`` 工厂，提供 Key / 词库 / 图片白名单 /
  审核日志 / 向量重建五组端点（X-Admin-Token 令牌认证）；
- ``api.dependencies``：令牌鉴权与分页公共依赖，供 ``admin`` 及后续 ``app``
  （审核 API :8000，T10 提供）复用。

本模块不主动导入子模块，避免包导入即拉起 FastAPI 依赖。
"""
