# 使用提醒:
# 1. xbot包提供软件自动化、数据表格、Excel、日志、AI等功能
# 2. package包提供访问当前应用数据的功能，如获取元素、访问全局变量、获取资源文件等功能
# 3. 当此模块作为流程独立运行时执行main函数
# 4. 可视化流程中可以通过"调用流程"的指令使用此模块

import xbot
from xbot import print
from . import package
from .package import variables as glv

import os
import re
import json
import dashscope
import pandas as pd
from datetime import datetime
from .config import QWEN_MODEL, QWEN_API_KEY, QWEN_URL
from dashscope.common.error import (
    DashScopeException, AuthenticationError, RequestFailure
)


class Cross_Check():
    # 初始化
    def __init__(self, path):
        self.RAW_DIR = path.get("RAW_DIR")
        self.LOG_DIR = path.get("LOG_DIR")
        self.TASK_DIR = path.get("TASK_DIR")
        self.OUTPUT_DIR = path.get("OUTPUT_DIR")
        self.ARCHIVE_DIR = path.get("ARCHIVE_DIR")
        self.ENABLE_AI_ANALYSIS = path.get("ENABLE_AI_ANALYSIS", True)
        self._api_state = {
            "enabled": True,
            "consecutive_failures": 0,
            "max_fail": 3
        }
        # 自动创建所有输出目录
        for dir_path in [self.LOG_DIR, self.TASK_DIR, self.OUTPUT_DIR, self.ARCHIVE_DIR]:
            if dir_path:
                try:
                    os.makedirs(dir_path, exist_ok=True)
                except Exception as e:
                    err_msg = f"创建目录失败 {dir_path}：{str(e)}"
                    self.write_log(err_msg)
                    self.write_fail_task(err_msg)
                    raise
        # 初始化日志文件路径
        self.today_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = os.path.join(self.LOG_DIR, f"run_{self.today_str}.log")
        # 启动时先写入一行日志
        self.write_log("=== 跨境对账任务启动 ===")

    # 读取原始文件
    def read_csv(self, filename):
        path = os.path.join(self.RAW_DIR, filename)
        self.write_log(f"读取文件：{filename}")
        try:
            df = pd.read_csv(path, encoding="utf-8")
        except Exception as e:
            err_msg = f"读取CSV文件失败 {filename}：{str(e)}"
            self.write_log(err_msg)
            self.write_fail_task(err_msg)
            raise
        # 空文件防御
        if df.empty:
            err_msg = f"原始文件 {filename} 为空，无法继续对账"
            self.write_log(err_msg)
            self.write_fail_task(err_msg)
            raise ValueError(err_msg)
        return df

    # 订单格式统一
    def unify_order_format(self, amazon_orders, temu_orders):
        self.write_log("开始执行订单数据标准化")
        try:
            amazon_order_lines = amazon_orders.copy()
            amazon_order_lines["line_id"] = amazon_order_lines["order_id"]
            amazon_order_lines["platform"] = "亚马逊"
            amazon_order_lines = amazon_order_lines[[
                "line_id", "platform", "order_id", "order_amount",
                "order_status", "order_date"
            ]]
            temu_order_lines = temu_orders.copy()
            temu_order_lines["line_id"] = temu_orders["sub_order_id"]
            temu_order_lines["platform"] = "Temu"
            temu_order_lines["order_amount"] = temu_order_lines["goods_amount"]
            temu_order_lines["order_date"] = temu_order_lines["create_time"]
            temu_order_lines = temu_order_lines[[
                "line_id", "platform", "order_id", "sub_order_id",
                "order_amount", "order_status", "order_date"
            ]]
            all_orders = pd.concat(
                [amazon_order_lines, temu_order_lines], ignore_index=True)
        except Exception as e:
            err_msg = f"订单格式标准化异常：{str(e)}"
            self.write_log(err_msg)
            self.write_fail_task(err_msg)
            raise
        self.write_log(f"订单标准化完成，总订单行数：{len(all_orders)}")
        return all_orders

    # 结算格式统一
    def unify_settle_format(self, amazon_settle, temu_settle):
        self.write_log("开始执行结算数据标准化")
        try:
            amazon_settle_lines = amazon_settle.copy()
            amazon_settle_lines["line_id"] = amazon_settle_lines["order_id"]
            amazon_settle_lines["platform"] = "亚马逊"
            amazon_settle_lines["settle_amount"] = amazon_settle_lines["total_settlement"]
            amazon_settle_lines["refund_amount"] = amazon_settle_lines["refund_amount"]
            amazon_settle_lines["platform_fee"] = amazon_settle_lines["platform_commission"]
            amazon_settle_lines["logistics_fee"] = amazon_settle_lines["fba_fee"]
            amazon_settle_lines["other_fee"] = amazon_settle_lines["other_fee"]
            amazon_settle_lines = amazon_settle_lines[[
                "line_id", "platform", "settlement_id", "order_id",
                "settle_amount", "refund_amount", "platform_fee",
                "logistics_fee", "other_fee", "settlement_date"
            ]]
            temu_settle_lines = temu_settle.copy()
            temu_settle_lines["line_id"] = temu_settle_lines["sub_order_id"]
            temu_settle_lines["platform"] = "Temu"
            temu_settle_lines["settlement_id"] = temu_settle_lines["statement_no"]
            temu_settle_lines["settle_amount"] = temu_settle_lines["actual_settlement"]
            temu_settle_lines["refund_amount"] = temu_settle_lines["refund_amt"]
            temu_settle_lines["platform_fee"] = temu_settle_lines["platform_service_fee"]
            temu_settle_lines["logistics_fee"] = temu_settle_lines["logistics_fee"]
            temu_settle_lines["other_fee"] = 0
            temu_settle_lines["settlement_date"] = temu_settle_lines["settle_time"]
            temu_settle_lines = temu_settle_lines[[
                "line_id", "platform", "settlement_id", "order_id", "sub_order_id",
                "settle_amount", "refund_amount", "platform_fee",
                "logistics_fee", "other_fee", "settlement_date"
            ]]
            # 合并全平台结算数据
            all_settlements = pd.concat(
                [amazon_settle_lines, temu_settle_lines], ignore_index=True)
        except Exception as e:
            err_msg = f"结算格式标准化异常：{str(e)}"
            self.write_log(err_msg)
            self.write_fail_task(err_msg)
            raise
        self.write_log(f"结算标准化完成，总结算行数：{len(all_settlements)}")
        return all_settlements

    # 第一层对账 -> 状态判断 -> 订单对结算
    def check_order_settle(self, row):
        # 优先级1：无结算记录
        if pd.isna(row["settlement_id"]):
            # 【优化】区分退款中订单和普通未结算
            if str(row["order_status"]) in ["退款中", "Refunded", "退款处理中"]:
                return "退款处理中"
            return "已下单未结算"
        # 优先级2：退款结算
        if round(float(row["refund_amount"]), 2) > 0:
            return "已退款结算"
        # 优先级3：金额校验（仅正向结算）
        expected = round(
            float(row["order_amount"]) - float(row["platform_fee"])
            - float(row["logistics_fee"]) - float(row["other_fee"]),
            2
        )
        if abs(expected - round(float(row["settle_amount"]), 2)) > 0.01:
            return "结算金额差异"
        return "结算正常"

    # 第二层对账 -> 订单状态 -> 结算对回款
    def check_batch_pay(self, row):
        # 无任何回款
        if pd.isna(row["到账总额"]):
            return "已结算未回款"

        settle_total = round(float(row["结算总额"]), 2)
        receive_total = round(float(row["到账总额"]), 2)
        diff = abs(settle_total - receive_total)
        # 全额到账
        if diff <= 0.01:
            return "回款正常"
        # 部分到账（回款小于结算，属于正常在途）
        if receive_total < settle_total:
            return "部分到账"
        # 回款大于结算，属于真实异常
        return "回款金额差异"

    # 正则表达式提取json
    def _extract_json(self, content: str) -> str:
        content = content.strip()
        try:
            json.loads(content)
            return content
        except json.JSONDecodeError:
            self.write_log(f"_extract_json JSONDecodeError:{content}")
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", content, re.DOTALL)
        if match:
            return match.group(1)
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end > start:
            return content[start:end+1]
        return content

    # 调用千问进行差异分析
    def qwen_analyze_diff(self, diff_type, row_data):
        if not self.ENABLE_AI_ANALYSIS:
            return "AI分析未开启"

        # 熔断器判断，连续失败达到阈值直接跳过调用
        if not self._api_state["enabled"]:
            return "AI服务已熔断，跳过分析"
        #
        system_content = "你是跨境电商财务对账专家，请根据对账差异信息，用一句话简短说明业务原因，控制在30字以内，不要换行，直接输出结果，不要多余解释。"
        user_content = f"""
            差异类型：{diff_type}
            详细数据：{json.dumps(row_data, ensure_ascii=False)}
        """
        try:
            resp = dashscope.MultiModalConversation.call(
                model=QWEN_MODEL,
                messages=[
                    {"role": "system", "content": [{"text": system_content}]},
                    {"role": "user", "content": [
                        {"text": user_content.strip()}]}
                ],
                temperature=0.1,
                result_format="message",
                timeout=20
            )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"API返回错误码:{resp.status_code},{resp.message}")
            output = resp.get("output", {})
            choices = output.get("choices", [])
            if not choices:
                raise RuntimeError("模型返回choices为空")
            content = choices[0]["message"]["content"]
            raw_text = content[0]["text"].strip()
            # 处理markdown包裹
            clean_text = self._extract_json(raw_text)
            # 本函数只返回普通文本，不是json，不需要loads，直接返回clean_text
            self._api_state["consecutive_failures"] = 0
            self._api_state["enabled"] = True
            # 去除AI返回文本多余的换行符和空格
            clean_text = clean_text.replace("\n", "").replace("\r", "").strip()
            return clean_text
        except AuthenticationError:
            self._api_state["enabled"] = False
            self.write_log("AI分析失败：API‑Key认证错误")
            return "AI分析失败：API‑Key认证错误"
        except RequestFailure:
            self._api_state["consecutive_failures"] += 1
            if self._api_state["consecutive_failures"] >= self._api_state["max_fail"]:
                self._api_state["enabled"] = False
            self.write_log("AI分析失败：服务/网络/超时异常")
            return f"AI分析失败：服务/网络/超时异常"
        except DashScopeException as e:
            self._api_state["consecutive_failures"] += 1
            if self._api_state["consecutive_failures"] >= self._api_state["max_fail"]:
                self._api_state["enabled"] = False
            self.write_log(f"AI分析异常：{str(e)[:25]}")
            return f"AI分析异常:{str(e)[:25]}"
        except Exception as e:
            self._api_state["consecutive_failures"] += 1
            if self._api_state["consecutive_failures"] >= self._api_state["max_fail"]:
                self._api_state["enabled"] = False
            self.write_log(f"AI分析失败:{str(e)[:25]}")
            return f"AI分析失败:{str(e)[:25]}"

    # 写入失败中转文件
    def write_fail_task(self, error_msg):
        try:
            task_data = {
                "日期": datetime.now().strftime("%Y-%m-%d"),
                "运行状态": "失败",
                "错误信息": error_msg,
                "日志文件路径": self.log_file
            }
            task_file = os.path.join(
                self.TASK_DIR, f"task_{self.today_str}.json")
            with open(task_file, "w", encoding="utf-8") as f:
                json.dump(task_data, f, ensure_ascii=False, indent=2)
            self.write_log(f"任务失败，错误信息：{error_msg}")
            self.write_log(f"失败任务文件已生成：{task_file}")
        except Exception as e:
            self.write_log(f"生成失败task.json本身出错：{str(e)}")

    # 写入成功中转文件
    def write_success_task(self, total_orders, settled_count, received_count, diff_count, diff_summary, result_file):
        try:
            task_data = {
                "日期": datetime.now().strftime("%Y-%m-%d"),
                "运行状态": "成功",
                "AI状态": self.ENABLE_AI_ANALYSIS,
                "订单总数": int(total_orders),
                "已结算订单数": int(settled_count),
                "全额到账批次": int(received_count),
                "差异总数量": int(diff_count),
                "差异摘要": diff_summary,
                "差异结果文件路径": result_file,
                "日志文件路径": self.log_file
            }
            task_file = os.path.join(
                self.TASK_DIR, f"task_{self.today_str}.json")
            with open(task_file, "w", encoding="utf-8") as f:
                json.dump(task_data, f, ensure_ascii=False, indent=2)

            self.write_log(f"任务执行完成，发现差异：{diff_count} 处")
            self.write_log(f"结果文件：{result_file}")
        except Exception as e:
            err_msg = f"写入成功task.json异常：{str(e)}"
            self.write_log(err_msg)
            self.write_fail_task(err_msg)
            raise

    # ========== 统一日志写入方法 ==========
    def write_log(self, msg):
        """写入一行日志到日志文件，同时控制台打印"""
        time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{time_str}] {msg}\n"
        # 追加写入文件
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(log_line)
        except Exception:
            pass
        # 控制台也输出，方便影刀调试
        print(log_line.strip())

    def main(self):
        try:
            # ============== 1. 读取5份原始报表 ==============
            amazon_orders = self.read_csv("亚马逊订单明细.csv")
            temu_orders = self.read_csv("Temu订单明细.csv")
            amazon_settle = self.read_csv("亚马逊结算账单.csv")
            temu_settle = self.read_csv("Temu结算账单.csv")
            payment = self.read_csv("收款流水报表.csv")
            self.write_log("全部原始文件读取完成")

            # ============== 2. 订单数据统一格式 ==============
            all_orders = self.unify_order_format(amazon_orders, temu_orders)
            self.write_log("订单格式统一完成")

            # ============== 3. 结算数据统一格式 ==============
            all_settlements = self.unify_settle_format(
                amazon_settle, temu_settle)
            self.write_log("结算订单格式统一完成")

            # ============== 4. 第一层对账：订单 ↔ 结算 ==============
            self.write_log("开始执行订单与结算表merge关联")
            try:
                # 合并订单和结算数据
                order_settle_merge = pd.merge(
                    all_orders,                                 # 左表 -> 订单
                    all_settlements,                            # 右表 -> 结算
                    on=["line_id", "platform", "order_id"],     # 合并条件
                    how="left",                                 # 左连接
                    suffixes=("_order", "_settle")
                )
            except Exception as e:
                err_msg = f"订单与结算merge合并失败：{str(e)}"
                self.write_log(err_msg)
                self.write_fail_task(err_msg)
                raise

            self.write_log("开始逐行判定订单‑结算对账状态")
            try:
                # 标记对账状态
                order_settle_merge["对账状态"] = order_settle_merge.apply(
                    self.check_order_settle, axis=1)
            except Exception as e:
                err_msg = f"订单结算状态apply计算异常：{str(e)}"
                self.write_log(err_msg)
                self.write_fail_task(err_msg)
                raise

            # 筛选异常订单（保留所有非结算正常的记录）
            order_settle_diff = order_settle_merge[
                ~order_settle_merge["对账状态"].isin(["结算正常", "已退款结算"])
            ].copy()
            self.write_log(f"订单结算差异行数：{len(order_settle_diff)}")

            # AI分析：订单结算差异
            if self.ENABLE_AI_ANALYSIS and len(order_settle_diff) > 0:
                self.write_log("开始AI批量生成订单差异备注")
                try:
                    ai_remarks = []
                    for _, row in order_settle_diff.iterrows():
                        row_dict = row.dropna().to_dict()
                        remark = self.qwen_analyze_diff(row["对账状态"], row_dict)
                        ai_remarks.append(remark)
                    order_settle_diff["AI业务备注"] = ai_remarks
                except Exception as e:
                    err_msg = f"订单差异AI批量处理异常：{str(e)}"
                    self.write_log(err_msg)
                    self.write_fail_task(err_msg)
                    raise
            else:
                order_settle_diff["AI业务备注"] = ""

            # ============== 5. 第二层对账：结算 ↔ 收款 ==============
            self.write_log("开始执行结算‑收款对账逻辑")
            try:
                # 先按批次汇总结算总额
                settle_summary = all_settlements.groupby(["settlement_id", "platform"])[
                    "settle_amount"].sum().reset_index()
                settle_summary.columns = ["settlement_id", "platform", "结算总额"]
                # 先按批次汇总所有渠道回款总额，用于主对账
                pay_batch_summary = payment.groupby("related_settlement")[
                    ["received_amount", "fee", "net_amount"]
                ].sum().reset_index()
                pay_batch_summary.columns = [
                    "settlement_id", "到账总额", "手续费总额", "净到账额"]

                # 链接结算表和回款表，进行批次级主对账（按总金额判断，避免分渠道假差异）
                batch_merge = pd.merge(
                    settle_summary,
                    pay_batch_summary,
                    on="settlement_id",
                    how="left"
                )
                batch_merge["对账状态"] = batch_merge.apply(
                    self.check_batch_pay, axis=1)

                # 关联回渠道明细，保留渠道维度信息，状态沿用批次级结果
                # 先整理渠道明细
                payment_channel = payment.groupby(["related_settlement", "payment_channel"])[
                    ["received_amount", "fee", "net_amount"]
                ].sum().reset_index()
                payment_channel.columns = [
                    "settlement_id", "收款渠道", "到账总额", "手续费总额", "净到账额"]
                # 渠道明细关联批次状态
                settle_pay_merge = pd.merge(
                    payment_channel,
                    batch_merge[["settlement_id", "platform", "结算总额", "对账状态"]],
                    on="settlement_id",
                    how="right"
                )
                # 筛选异常回款（保留所有非回款正常的批次）
                settle_pay_diff = settle_pay_merge[
                    settle_pay_merge["对账状态"] != "回款正常"
                ].copy()
            except Exception as e:
                err_msg = f"结算‑收款对账分组/合并逻辑异常：{str(e)}"
                self.write_log(err_msg)
                self.write_fail_task(err_msg)
                raise
            self.write_log(f"结算回款差异行数：{len(settle_pay_diff)}")

            # AI分析：结算回款差异
            if self.ENABLE_AI_ANALYSIS and len(settle_pay_diff) > 0:
                self.write_log("开始AI批量生成回款差异备注")
                try:
                    ai_remarks = []
                    for _, row in settle_pay_diff.iterrows():
                        row_dict = row.dropna().to_dict()
                        remark = self.qwen_analyze_diff(row["对账状态"], row_dict)
                        ai_remarks.append(remark)
                    settle_pay_diff["AI业务备注"] = ai_remarks
                except Exception as e:
                    err_msg = f"回款差异AI批量处理异常：{str(e)}"
                    self.write_log(err_msg)
                    self.write_fail_task(err_msg)
                    raise
            else:
                settle_pay_diff["AI业务备注"] = ""

            # ============== 6. 输出对账结果Excel ==============
            today_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_file = os.path.join(
                self.OUTPUT_DIR, f"对账差异_{today_str}.xlsx")
            self.write_log(f"正在导出结果Excel：{output_file}")
            try:
                with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
                    order_settle_diff.to_excel(
                        writer, sheet_name="订单结算差异", index=False)
                    settle_pay_diff.to_excel(
                        writer, sheet_name="结算回款差异", index=False)
            except Exception as e:
                err_msg = f"导出Excel失败（可能文件被占用/权限不足）：{str(e)}"
                self.write_log(err_msg)
                self.write_fail_task(err_msg)
                raise

            # ============== 7. 生成中转任务JSON ==============
            total_orders = len(all_orders)
            # 已结算订单数（有结算记录的订单）
            settled_count = int(
                order_settle_merge["settlement_id"].notna().sum())
            # 全额到账的结算批次数量
            received_count = int((batch_merge["对账状态"] == "回款正常").sum())
            # 订单差异行数 + 回款侧去重后的差异批次数量
            order_diff_cnt = len(order_settle_diff)
            pay_diff_cnt = len(
                settle_pay_diff[["settlement_id", "对账状态"]].drop_duplicates())
            diff_count = order_diff_cnt + pay_diff_cnt
            # 订单侧差异统计
            diff_summary = []
            if not order_settle_diff.empty:
                for status, cnt in order_settle_diff["对账状态"].value_counts().items():
                    diff_summary.append(f"{status}：{cnt}笔")
            # 回款侧差异统计（按批次去重统计）
            pay_diff_stats = settle_pay_diff[[
                "settlement_id", "对账状态"]].drop_duplicates()
            if not pay_diff_stats.empty:
                for status, cnt in pay_diff_stats["对账状态"].value_counts().items():
                    diff_summary.append(f"{status}：{cnt}笔")
            # 拼接差异excel文件路径
            result_file = os.path.join(
                self.OUTPUT_DIR, f"对账差异_{self.today_str}.xlsx")
            # 写入成功中转文件
            self.write_success_task(total_orders, settled_count,
                                    received_count, diff_count, diff_summary, result_file)

            # 控制台输出
            print("="*50)
            print("跨境对账执行完成")
            print(f"AI分析：{'开启' if self.ENABLE_AI_ANALYSIS else '关闭'}")
            print(f"总订单数：{total_orders}")
            print(f"已结算订单：{settled_count}")
            print(f"全额到账批次：{received_count}")
            print(f"发现差异：{diff_count} 处")
            print(f"差异结果文件：{output_file}")
            print(
                f"中转任务文件：{os.path.join(self.TASK_DIR, f'task_{self.today_str}.json')}")
            print("="*50)
        except Exception as e:
            error_info = str(e)
            self.write_fail_task(error_info)
            self.write_log(error_info)
            raise


def main(args):
    cck = Cross_Check(args)
    cck.main()


# if __name__ == "__main__":
#     main({
#         'RAW_DIR': 'E:\\RPA简历项目\\跨境多平台订单&结算对账自动化系统\\后台数据',
#         'ARCHIVE_DIR': 'E:\\RPA简历项目\\跨境多平台订单&结算对账自动化系统\\archive',
#         'OUTPUT_DIR': 'E:\\RPA简历项目\\跨境多平台订单&结算对账自动化系统\\output',
#         'TASK_DIR': 'E:\\RPA简历项目\\跨境多平台订单&结算对账自动化系统\\task',
#         'LOG_DIR': 'E:\\RPA简历项目\\跨境多平台订单&结算对账自动化系统\\log',
#         'ENABLE_AI_ANALYSIS': False,
#     })
