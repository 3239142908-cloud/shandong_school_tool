"""闪动校园 8.7.7 单目录启动器。

用法:
    run.cmd --list
    run.cmd login_cli --help
    run.cmd login_cli -u <手机号> -p <密码>
    run.cmd run_cli -t free -d 2000 --lat 36.67 --lng 117.05
    run.cmd smoke_test

工具名可省略 .py 后缀；"--" 会被忽略，其余参数原样透传。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CLIS = (
    "login_cli", "user_cli", "policy_cli", "score_cli", "rank_cli", "history_cli",
    "run_cli", "ai_cli", "face_cli", "task_cli", "community_cli", "alumni_cli",
    "message_cli",
)


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0

    if argv[0] in ("--list", "-l", "list"):
        print("可用工具:")
        for cli in CLIS:
            print(f"  - {cli}.py")
        print("  - smoke_test_877.py  (冒烟测试)")
        return 0

    argv = [a for a in argv if a != "--"]
    if not argv:
        print(__doc__.strip())
        return 2

    tool = argv[0]
    if tool in ("smoke", "smoke_test"):
        target = os.path.join(HERE, "smoke_test_877.py")
    else:
        target = os.path.join(HERE, tool if tool.endswith(".py") else f"{tool}.py")

    if not os.path.exists(target):
        print(f"[错误] 目标不存在: {target}")
        print("用 --list 查看可用工具。")
        return 2

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.call([sys.executable, target, *argv[1:]], cwd=HERE, env=env)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
