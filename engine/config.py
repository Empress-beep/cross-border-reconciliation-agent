"""引擎配置：读取 .env，缺失只警告不报错（对应功能降级）。"""

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv 缺失时退化为纯环境变量
    load_dotenv = None

# 项目根目录（engine/ 的上一级），.env 固定放根目录
ROOT_DIR = Path(__file__).resolve().parents[1]

if load_dotenv is not None:
    load_dotenv(ROOT_DIR / ".env")

# ===================== 千问（内置AI归因） =====================
QWEN_API_KEY = os.getenv("DASHSCOPE_API_KEY", "").strip()
QWEN_MODEL = "qwen3.8-max"
# 新版调用可携带业务空间ID（从环境变量读取；不配置则调用不带 workspace_id）
QWEN_WORKSPACE_ID = os.getenv("DASHSCOPE_WORKSPACE_ID", "").strip()
QWEN_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# ===================== FastGPT（Agent 聊天面板） =====================
FASTGPT_BASE_URL = os.getenv("FASTGPT_BASE_URL", "").strip().rstrip("/")
FASTGPT_API_KEY = os.getenv("FASTGPT_API_KEY", "").strip()
FASTGPT_APP_ID = os.getenv("FASTGPT_APP_ID", "").strip()


def fastgpt_configured() -> bool:
    """FastGPT 三项配置是否齐全（决定聊天面板是否渲染）。"""
    return bool(FASTGPT_BASE_URL and FASTGPT_API_KEY and FASTGPT_APP_ID)


def load_settings() -> dict:
    """汇总配置并给出降级警告（供 Flask 启动时调用）。"""
    warnings = []
    if not QWEN_API_KEY:
        warnings.append("未配置 DASHSCOPE_API_KEY，内置AI差异归因将降级关闭")
    if not fastgpt_configured():
        warnings.append("未配置 FASTGPT_BASE_URL / FASTGPT_API_KEY / FASTGPT_APP_ID，Agent聊天面板将隐藏")
    return {
        "DASHSCOPE_API_KEY": QWEN_API_KEY,
        "QWEN_MODEL": QWEN_MODEL,
        "QWEN_WORKSPACE_ID": QWEN_WORKSPACE_ID,
        "FASTGPT_BASE_URL": FASTGPT_BASE_URL,
        "FASTGPT_API_KEY": FASTGPT_API_KEY,
        "FASTGPT_APP_ID": FASTGPT_APP_ID,
        "FASTGPT_CONFIGURED": fastgpt_configured(),
        "warnings": warnings,
    }
