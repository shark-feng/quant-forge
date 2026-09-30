"""统一异常类型。

设计原则：异常必须能定位到「哪一段数据 / 哪一笔订单 / 哪一个配置」出错。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AQSError",
    "ConfigError",
    "DataError",
    "SchemaError",
    "DataQualityError",
    "LookaheadError",
    "FutureFunctionError",
    "InsufficientDataError",
    "EngineError",
    "OrderRejectedError",
    "RiskError",
]


class AQSError(Exception):
    """所有自定义异常的基类。"""


class ConfigError(AQSError):
    """配置缺失、类型错误或存在未知键。"""

    def __init__(self, message: str, *, path: str | None = None, value: Any = None) -> None:
        self.path = path
        self.value = value
        detail = message
        if path:
            detail = f"[配置项 {path}] {message}"
        if value is not None:
            detail = f"{detail}（实际值：{value!r}）"
        super().__init__(detail)


class DataError(AQSError):
    """数据层通用错误。"""


class SchemaError(DataError):
    """列缺失、类型不符、主键重复等结构性错误。"""


class DataQualityError(DataError):
    """数据质量校验未通过（strict 模式下抛出）。"""

    def __init__(self, message: str, *, report: Any = None) -> None:
        self.report = report
        super().__init__(message)


class LookaheadError(DataError):
    """试图访问决策时间之后的数据 —— 未来函数。

    这是本项目最严重的错误类型：一旦抛出，说明回测结果不可信。
    """

    def __init__(self, message: str, *, as_of: Any = None, requested: Any = None) -> None:
        self.as_of = as_of
        self.requested = requested
        super().__init__(
            f"未来函数违规：{message}（as_of={as_of}, requested={requested}）"
        )


class FutureFunctionError(AQSError):
    """订单的时间语义违反 T+1（信号日 ≥ 提交日）。"""


class InsufficientDataError(DataError):
    """请求的历史长度不足（例如上市不足窗口期）。"""


class EngineError(AQSError):
    """回测引擎内部错误。"""


class OrderRejectedError(EngineError):
    """订单被拒（非风控原因，例如不合规的数量）。"""


class RiskError(AQSError):
    """风控层错误。"""
