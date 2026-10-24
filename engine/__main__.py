"""命令行入口：python -m engine --raw 后台数据 --output output --ai 0"""

import argparse
import sys
from datetime import datetime

from .reconcile import run


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="跨境多平台订单 & 结算对账引擎（命令行模式）")
    parser.add_argument("--raw", required=True,
                        help="原始数据目录（含5份CSV），如：后台数据")
    parser.add_argument("--output", required=True,
                        help="结果输出目录，如：output")
    parser.add_argument("--ai", default="0", choices=["0", "1"],
                        help="AI差异分析开关：1=开启 0=关闭（默认0）")
    parser.add_argument("--log", default=None,
                        help="日志目录（run.log 落盘位置；不传则只输出控制台）")
    parser.add_argument("--task-id", default="",
                        help="任务ID（不传则按时间自动生成）")
    args = parser.parse_args(argv)

    task_id = args.task_id or datetime.now().strftime("task_%Y%m%d_%H%M%S")
    result = run(
        raw_dir=args.raw,
        output_dir=args.output,
        ai_enabled=(args.ai == "1"),
        log_dir=args.log,
        task_id=task_id,
    )

    print("=" * 50)
    if result["status"] == "success":
        stats = result["stats"]
        print("跨境对账执行完成")
        print(f"任务ID：{task_id}")
        print(f"AI分析：{'开启' if result['ai_enabled'] else '关闭'}")
        print(f"总订单数：{stats['total_orders']}")
        print(f"已结算订单：{stats['settled_count']}")
        print(f"全额到账批次：{stats['received_count']}")
        print(f"发现差异：{stats['diff_count']} 处")
        print(f"差异结果文件：{result['excel_path']}")
        print(f"总耗时：{result['elapsed_ms']}ms")
    else:
        print("跨境对账执行失败")
        print(f"任务ID：{task_id}")
        print(f"失败步骤：{result['step']}")
        print(f"错误信息：{result['error']}")
        if args.log:
            print(f"日志文件：{args.log}/run.log")
    print("=" * 50)
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
