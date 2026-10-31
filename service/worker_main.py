"""RQ worker 入口：python -m service.worker_main（由 Web 进程守护拉起）。

职责单一：连 Redis 取队列里的任务 → 调 task_runner.run_task_job 执行。
不做启动恢复（唯一责任方是 Web），不启看门狗（状态在 MySQL，Web 侧扫）。
"""

import logging

from rq import SimpleWorker

from service import bootstrap, task_runner


def main():
    bootstrap.init_worker()
    queue = task_runner.get_queue()
    lg = logging.getLogger("app")
    lg.info("RQ worker 开始消费：queue=%s redis=%s:%s/%s",
            queue.name,
            task_runner.get_connection().connection_pool.connection_kwargs.get("host"),
            task_runner.get_connection().connection_pool.connection_kwargs.get("port"),
            task_runner.get_connection().connection_pool.connection_kwargs.get("db"),
    )
    # Windows 无 os.fork：必须用 SimpleWorker（job 在 worker 进程内直接执行，
    # 不 fork 子进程）。job 顶层异常由 run_task_job 捕获并回写 failed。
    SimpleWorker([queue], connection=task_runner.get_connection()).work()


if __name__ == "__main__":
    main()
