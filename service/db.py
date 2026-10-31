"""MySQL 连接（线程本地 + 断线重连）与库表初始化。

连接按线程隔离：Flask 多线程与看门狗线程各用各的连接，autocommit 简化事务心智。
"""

import threading

import pymysql

from service import settings

_local = threading.local()


def _connect(db=None):
    return pymysql.connect(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        database=db,
        charset="utf8mb4",
        autocommit=True,
        connect_timeout=5,
        cursorclass=pymysql.cursors.DictCursor,
    )


def get_conn():
    """取当前线程的连接；已有连接先 ping，断了自动重连一次。"""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.ping()
            return conn
        except Exception:
            _local.conn = None
    try:
        conn = _connect(settings.MYSQL_DB)
    except pymysql.MySQLError as e:
        raise RuntimeError(
            "MySQL 连接失败（{}:{} 库 {}）：{}。请确认 MySQL 服务已启动，"
            "并检查 .env 的 MYSQL_HOST/PORT/USER/PASSWORD。".format(
                settings.MYSQL_HOST, settings.MYSQL_PORT,
                settings.MYSQL_DB, e)) from e
    _local.conn = conn
    return conn


def init_schema():
    """建库建表（幂等）。失败抛 RuntimeError，启动即报错不留隐患。"""
    try:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE DATABASE IF NOT EXISTS `{}` "
                    "DEFAULT CHARACTER SET utf8mb4".format(settings.MYSQL_DB))
            conn.select_db(settings.MYSQL_DB)
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS tasks (
                        task_id     VARCHAR(40)  NOT NULL PRIMARY KEY,
                        status      VARCHAR(16)  NOT NULL DEFAULT 'pending',
                        ai_enabled  TINYINT      NOT NULL DEFAULT 0,
                        step        VARCHAR(64)  NULL,
                        error       TEXT         NULL,
                        created_at  VARCHAR(19)  NOT NULL,
                        started_at  VARCHAR(19)  NULL,
                        finished_at VARCHAR(19)  NULL,
                        elapsed_ms  INT          NULL,
                        excel_file  VARCHAR(255) NULL,
                        diff_count  INT          NOT NULL DEFAULT 0,
                        payload     LONGTEXT     NULL,
                        KEY idx_status (status),
                        KEY idx_created (created_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """)
        finally:
            conn.close()
    except pymysql.MySQLError as e:
        raise RuntimeError(
            "MySQL 初始化失败（{}:{}）：{}。请确认 MySQL 服务已启动，"
            "并检查 .env 的 MYSQL_* 配置。".format(
                settings.MYSQL_HOST, settings.MYSQL_PORT, e)) from e


def ping():
    """健康检查：能连返回 True。"""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        return True
    except Exception:
        return False
