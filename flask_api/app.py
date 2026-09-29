from flask import Flask, request, jsonify
import json
import os
import time
import logging

app = Flask(__name__)
# -------------------------- 配置区，请修改为你本机真实项目路径 --------------------------
TASK_FOLDER = r"E:\RPA简历项目\跨境多平台订单&结算对账自动化系统\global_task_in"
LOG_FILE = r"E:\RPA简历项目\跨境多平台订单&结算对账自动化系统\log\flask_api.log"
# --------------------------------------------------------------------------------

# 初始化日志
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    encoding="utf-8"
)

os.makedirs(TASK_FOLDER, exist_ok=True)


@app.route("/submit_task", methods=["POST"])
def create_rpa_task():
    """提交对账任务，生成task_id中转文件（原有接口保持不变）"""
    try:
        raw_data = request.get_json()
        logging.info(f"收到Agent请求，原始入参: {raw_data}")
        file_path = raw_data.get("file_path")
        ai_analysis = raw_data.get("ai_analysis")

        if not file_path:
            logging.warning("参数错误：file_path为空")
            return jsonify({"code": 400, "msg": "file_path不能为空", "task_id": None}), 400
        if ai_analysis not in (0, 1):
            logging.warning(f"参数错误：ai_analysis={ai_analysis}，只允许0或1")
            return jsonify({"code": 400, "msg": "ai_analysis只能为0或1", "task_id": None}), 400

        task_id = f"task_{int(time.time())}"
        task_content = {
            "task_id": task_id,
            "file_path": file_path,
            "ai_analysis": ai_analysis,
            "create_time": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        task_file_path = os.path.join(TASK_FOLDER, f"{task_id}.json")
        with open(task_file_path, "w", encoding="utf-8") as f:
            json.dump(task_content, f, ensure_ascii=False, indent=2)

        logging.info(f"任务文件已生成:{task_file_path}, task_id:{task_id}")
        return jsonify({
            "code": 200,
            "msg": "任务已提交，等待RPA执行",
            "task_id": task_id
        }), 200
    except Exception as e:
        err_msg = str(e)
        logging.error(f"接口异常:{err_msg}")
        return jsonify({"code": 500, "msg": f"服务异常:{err_msg}", "task_id": None}), 500


@app.route("/get_task_result", methods=["POST"])
def get_task_result():
    """【新增接口】根据task_id查询RPA对账任务结果"""
    try:
        req_data = request.get_json()
        task_id = req_data.get("task_id")
        logging.info(f"查询任务状态，task_id={task_id}")

        if not task_id:
            logging.warning("查询接口参数错误：task_id为空")
            return jsonify({
                "code": 400,
                "msg": "task_id不能为空"
            }), 400

        result_file = os.path.join(TASK_FOLDER, f"{task_id}_result.json")
        # 结果文件还不存在 → 任务正在运行
        if not os.path.exists(result_file):
            return jsonify({
                "code": 200,
                "data": {
                    "task_id": task_id,
                    "status": "running",
                    "msg": "RPA任务正在执行，请稍后查询"
                }
            }), 200

        # 存在结果文件，读取并返回
        with open(result_file, "r", encoding="utf-8") as f:
            result_data = json.load(f)

        logging.info(f"查询到任务结果 task_id={task_id}, status={result_data.get('status')}")
        return jsonify({
            "code": 200,
            "data": result_data
        }), 200

    except Exception as e:
        err_msg = str(e)
        logging.error(f"查询任务接口异常: {err_msg}")
        return jsonify({
            "code": 500,
            "msg": f"查询服务异常：{err_msg}"
        }), 500


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=8765, debug=False)
