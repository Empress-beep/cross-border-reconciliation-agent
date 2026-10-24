"""内置AI差异归因：脱敏后调用千问，带熔断器（自 cross_check.py 等价迁移）。

归因只是对**已判定**的差异补充一句建议性备注，不参与任何对账判定。
AI 不可用（缺key/断网/连续失败）时返回占位文案，绝不让任务失败。
"""

import json
import logging
import re
import time

from .config import QWEN_API_KEY, QWEN_MODEL, QWEN_WORKSPACE_ID
from .desensitize import sanitize_row

_SYSTEM_PROMPT = (
    "你是跨境电商财务对账专家，请根据对账差异信息，用一句话简短说明业务原因，"
    "控制在30字以内，不要换行，直接输出结果，不要多余解释。"
)


class Attributor:
    """千问差异归因器：每个任务实例化一个，持有独立熔断状态。"""

    def __init__(self, enabled: bool, logger: logging.Logger = None):
        self.enabled = bool(enabled)
        self.logger = logger or logging.getLogger("engine")
        self._call_no = 0
        self._api_state = {
            "enabled": True,
            "consecutive_failures": 0,
            "max_fail": 3
        }
        if self.enabled and not QWEN_API_KEY:
            self.logger.warning("未配置 DASHSCOPE_API_KEY，AI归因降级关闭")
            self.enabled = False

    # ---------- 熔断状态维护 ----------

    def _mark_success(self):
        self._api_state["consecutive_failures"] = 0
        self._api_state["enabled"] = True

    def _mark_failure(self, reason: str):
        st = self._api_state
        st["consecutive_failures"] += 1
        self.logger.warning(
            "AI归因失败(%d/%d)：%s",
            st["consecutive_failures"], st["max_fail"], reason
        )
        if st["consecutive_failures"] >= st["max_fail"] and st["enabled"]:
            st["enabled"] = False
            self.logger.error(
                "AI归因熔断：连续失败%d次，本次任务后续调用全部跳过",
                st["consecutive_failures"]
            )

    # ---------- markdown/json 文本规整 ----------

    def _extract_json(self, content: str) -> str:
        content = content.strip()
        try:
            json.loads(content)
            return content
        except json.JSONDecodeError:
            pass
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", content, re.DOTALL)
        if match:
            return match.group(1)
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end > start:
            return content[start:end + 1]
        return content

    # ---------- 归因主入口 ----------

    def qwen_analyze_diff(self, diff_type, row_data) -> str:
        """对单条差异生成 ≤30 字业务原因备注。入参先脱敏再出网。"""
        if not self.enabled:
            return "AI分析未开启"
        if not self._api_state["enabled"]:
            return "AI服务已熔断，跳过分析"

        safe_row = sanitize_row(row_data)
        user_content = (
            f"\n            差异类型：{diff_type}\n"
            f"            详细数据：{json.dumps(safe_row, ensure_ascii=False, default=str)}\n        "
        )
        self._call_no += 1
        self.logger.info(
            "[AI] 归因调用 #%d 状态=%s 脱敏后入参=%s",
            self._call_no, diff_type,
            json.dumps(safe_row, ensure_ascii=False, default=str)
        )
        t0 = time.time()
        try:
            import dashscope
            from dashscope.common.error import (
                DashScopeException, AuthenticationError, RequestFailure
            )
        except ImportError as e:
            self._mark_failure(f"dashscope未安装: {e}")
            return f"AI分析失败:dashscope未安装"

        try:
            call_kwargs = {
                "model": QWEN_MODEL,
                "messages": [
                    {"role": "system", "content": [{"text": _SYSTEM_PROMPT}]},
                    {"role": "user", "content": [
                        {"text": user_content.strip()}]}
                ],
                "temperature": 0.1,
                "result_format": "message",
                "timeout": 20,
            }
            if QWEN_WORKSPACE_ID:
                # 新版千问调用：配置了业务空间ID才携带（分发版默认不带账号信息）
                call_kwargs["workspace_id"] = QWEN_WORKSPACE_ID
            resp = dashscope.MultiModalConversation.call(**call_kwargs)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"API返回错误码:{resp.status_code},{resp.message}")
            output = resp.get("output", {})
            choices = output.get("choices", [])
            if not choices:
                raise RuntimeError("模型返回choices为空")
            content = choices[0]["message"]["content"]
            raw_text = content[0]["text"].strip()
            clean_text = self._extract_json(raw_text)
            clean_text = clean_text.replace("\n", "").replace("\r", "").strip()
            self._mark_success()
            self.logger.info(
                "[AI] 归因成功 状态=%s 耗时=%.1fs 备注=%s",
                diff_type, time.time() - t0, clean_text
            )
            return clean_text
        except AuthenticationError:
            self._api_state["enabled"] = False
            self.logger.error("AI归因失败：API-Key认证错误，本次任务后续调用全部跳过")
            return "AI分析失败：API-Key认证错误"
        except RequestFailure as e:
            self._mark_failure(f"服务/网络/超时异常: {e}")
            return "AI分析失败：服务/网络/超时异常"
        except DashScopeException as e:
            self._mark_failure(str(e)[:25])
            return f"AI分析异常:{str(e)[:25]}"
        except Exception as e:
            self._mark_failure(str(e)[:25])
            return f"AI分析失败:{str(e)[:25]}"
