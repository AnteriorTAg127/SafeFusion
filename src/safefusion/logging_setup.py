"""结构化日志：JSON 行格式输出到 stdout，级别可配置。

- ``setup_logging(config)`` 按 ``AppConfig.logging``（级别 / JSON 开关）初始化根日志器（幂等）；
- ``get_logger(name)`` 返回自动带 ``safefusion.`` 命名空间前缀的日志器。
"""

import json
import logging
import sys
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import AppConfig

#: 统一日志命名空间
_LOGGER_NAME = "safefusion"


class JsonLineFormatter(logging.Formatter):
    """JSON 行格式化器：每行一个 JSON 对象，字段含 ts / level / logger / message。"""

    def format(self, record: logging.LogRecord) -> str:
        """将日志记录序列化为单行 JSON。"""

        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # 透传可选业务字段（如 request_id），便于链路追踪
        for key in ("request_id",):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(config: "AppConfig") -> None:
    """按配置初始化根日志器（幂等：重复调用会替换 handler 与级别）。

    Args:
        config: 已加载的应用配置，取其 ``logging`` 分组（level / json_lines）。
    """

    root = logging.getLogger()
    level_name = (config.logging.level or "INFO").upper()
    root.setLevel(getattr(logging, level_name, logging.INFO))
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    if config.logging.json_lines:
        handler.setFormatter(JsonLineFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """获取项目日志器：自动补全 ``safefusion.`` 命名空间前缀。

    Args:
        name: 模块名（如 "engines.semantic"）。

    Returns:
        命名空间化的 logging.Logger 实例。
    """

    if not name.startswith(_LOGGER_NAME):
        name = f"{_LOGGER_NAME}.{name}"
    return logging.getLogger(name)
