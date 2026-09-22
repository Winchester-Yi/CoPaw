# -*- coding: utf-8 -*-
"""Monitor 服务启动入口.

使用独立虚拟环境 venv_monitor 运行，避免与其他服务包冲突。
"""

import os
import subprocess
import sys

# 服务配置
SERVICE_NAME = "monitor"
VENV_DIR = "venv_monitor"
PORT = 9090


def get_venv_python():
    """获取虚拟环境的 Python 路径."""
    if sys.platform == "win32":
        return os.path.join(VENV_DIR, "Scripts", "python.exe")
    return os.path.join(VENV_DIR, "bin", "python")


def ensure_venv():
    """确保虚拟环境存在并安装依赖."""
    venv_python = get_venv_python()

    if not os.path.exists(venv_python):
        print(f"[{SERVICE_NAME}] 创建虚拟环境 {VENV_DIR}...")
        subprocess.check_call([sys.executable, "-m", "venv", VENV_DIR])
        print(f"[{SERVICE_NAME}] 虚拟环境创建完成")

    # 安装依赖
    print(f"[{SERVICE_NAME}] 安装依赖...")
    subprocess.check_call(
        [venv_python, "-m", "pip", "install", "-e", "./monitor", "--quiet"],
    )
    print(f"[{SERVICE_NAME}] 依赖安装完成")


def run_in_venv():
    """在虚拟环境中启动服务."""
    # 添加 monitor/src 目录到 PYTHONPATH
    src_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "monitor",
        "src",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = src_path
    # 设置日志级别：debug 时打印所有 SQL
    env["MONITOR_LOG_LEVEL"] = "debug"

    venv_python = get_venv_python()

    # 使用虚拟环境的 Python 运行实际服务
    subprocess.check_call(
        [
            venv_python,
            "-m",
            "uvicorn",
            "monitor.app._app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
            "--log-level",
            "info",
        ],
        env=env,
    )


if __name__ == "__main__":
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)

    ensure_venv()
    print(f"[{SERVICE_NAME}] 启动服务，端口: {PORT}")
    run_in_venv()
