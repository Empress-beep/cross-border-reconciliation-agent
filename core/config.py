# 使用提醒:
# 注意：部署时请修改BASE_DIR为本机实际项目根目录
# 1. xbot包提供软件自动化、数据表格、Excel、日志、AI等功能
# 2. package包提供访问当前应用数据的功能，如获取元素、访问全局变量、获取资源文件等功能
# 3. 当此模块作为流程独立运行时执行main函数
# 4. 可视化流程中可以通过"调用流程"的指令使用此模块

import xbot
from xbot import print, sleep
from .import package
from .package import variables as glv

import os
from cross_check import Cross_Check

# ===================== 配置区 =====================

# AI 分析开关
# ENABLE_AI_ANALYSIS = True  # True=开启AI分析；False=关闭，只对账
# 千问API配置
QWEN_API_KEY = os.getenv("DASHSCOPE_API_KEY")
QWEN_MODEL = "qwen3.7-flash"
QWEN_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def main(args):
    # AI 分析开关
    if args.get("AI开关") != 0:
        ENABLE_AI_ANALYSIS = True  # True=开启AI分析；
    else:
        ENABLE_AI_ANALYSIS = False  # False=关闭，只对账

    BASE_DIR = args.get("文件地址")
    if not BASE_DIR or not isinstance(BASE_DIR, str):
        Cross_Check.write_log('BASE_DIR业务目录不能为空')
        raise ValueError("BASE_DIR业务根目录不能为空")
    RAW_DIR = os.path.join(BASE_DIR, "后台数据")
    ARCHIVE_DIR = os.path.join(BASE_DIR, "archive")
    OUTPUT_DIR = os.path.join(BASE_DIR, "output")
    TASK_DIR = os.path.join(BASE_DIR, "task")
    LOG_DIR = os.path.join(BASE_DIR, "log")
    
    # 自动创建目录, 如果有自动跳过
    os.makedirs(RAW_DIR, exist_ok=True)

    return {"RAW_DIR": RAW_DIR, "ARCHIVE_DIR": ARCHIVE_DIR,"OUTPUT_DIR": OUTPUT_DIR,"TASK_DIR":TASK_DIR,"LOG_DIR": LOG_DIR, "ENABLE_AI_ANALYSIS": ENABLE_AI_ANALYSIS}