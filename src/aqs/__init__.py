"""``aqs`` —— A 股量化研究系统（研究与教育用途，不构成投资建议）。

分层：
- :mod:`aqs.core`    基础设施：枚举/模型/事件/日历/异常/日志
- :mod:`aqs.config`  配置：强类型 schema 与加载器
- :mod:`aqs.data`    数据层：PIT 存储、复权、股票池、质量校验
- :mod:`aqs.engine`  回测引擎：成本、撮合、账务、事件循环
- :mod:`aqs.strategy` / :mod:`aqs.portfolio` / :mod:`aqs.risk`  策略/组合/风控（接口已定义）
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__", "RESEARCH_ONLY_NOTICE"]

RESEARCH_ONLY_NOTICE = (
    "本系统仅用于量化研究与教育，不构成投资建议；实盘交易需符合证监会、交易所与券商合规要求。"
    "第三阶段（Tick/订单簿、最优执行、蒙特卡洛）仅作模拟研究，不得直接实盘。"
)
