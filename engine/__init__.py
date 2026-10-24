"""跨境多平台订单 & 结算对账引擎（v2.0，自影刀内嵌代码剥离）。

对外入口：run(raw_dir, output_dir, ai_enabled, log_dir, task_id)
"""

from .reconcile import run

__all__ = ["run"]
