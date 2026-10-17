"""闪动校园 8.7.7 — 一键启动控制台。

双击同目录下的 `启动.cmd` 即可运行；也可直接:
    <venv>\\Scripts\\python.exe menu.py
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SESSION = os.path.join(HERE, "session.json")

# ---- ANSI 彩色（Windows 10+ 控制台） -------------------------------------
if os.name == "nt":
    os.system("")  # 打开 VT 处理
C = {
    "reset": "\033[0m", "dim": "\033[90m", "red": "\033[91m", "green": "\033[92m",
    "yellow": "\033[93m", "blue": "\033[96m", "bold": "\033[1m", "inv": "\033[7m",
}


def paint(text, *styles):
    return "".join(C.get(s, "") for s in styles) + str(text) + C["reset"]


# ---- 会话状态 -------------------------------------------------------------
def session_state():
    try:
        with open(SESSION, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None, "未找到 session.json"
    token = str(data.get("token") or "")
    if not token or token.startswith("YOUR_"):
        return data, "未登录（session.json 仍是占位模板）"
    return data, f"已登录  userId={data.get('userId') or '?'}  token={token[:10]}..."


# ---- 执行 -----------------------------------------------------------------
def run(*args, label=None):
    """执行子进程并暂停。"""
    cmd = [sys.executable, os.path.join(HERE, args[0] + ".py"), *args[1:]]
    print()
    print(paint("  ── 执行 ────────────────────────────────────────────", "dim"))
    print(paint("  等价命令: run.cmd " + " ".join(mask(args)), "dim"))
    print(paint("  ────────────────────────────────────────────────────", "dim"))
    print()
    try:
        code = subprocess.call(cmd, cwd=HERE, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except KeyboardInterrupt:
        print()
        print(paint("  [中断] 已取消当前操作", "yellow"))
        code = 130
    print()
    if code == 0:
        print(paint("  [完成] 退出码 0", "green"))
    else:
        print(paint(f"  [注意] 退出码 {code}", "yellow"))
    pause()


def mask(args):
    out, hide = [], False
    for a in args:
        if hide:
            out.append("******")
            hide = False
            continue
        out.append(a)
        if a in ("-p", "--password"):
            hide = True
    return out


def ask(prompt, default=""):
    try:
        value = input(paint(f"  {prompt}" + (f" [{default}]" if default else "") + ": ", "blue")).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    return value or default


def pause():
    try:
        input(paint("  按回车返回菜单 ...", "dim"))
    except (EOFError, KeyboardInterrupt):
        print()


def clear():
    os.system("cls" if os.name == "nt" else "clear")


# ---- 各菜单动作 -----------------------------------------------------------
def act_login():
    print()
    mobile = ask("手机号")
    if not mobile:
        print(paint("  已取消", "yellow"))
        return pause()
    try:
        import getpass
        password = getpass.getpass(paint("  密码（不回显）: ", "blue"))
    except Exception:
        password = ask("密码")
    if not password:
        print(paint("  已取消", "yellow"))
        return pause()
    run("login_cli", "-u", mobile, "-p", password)


def act_credential():
    print()
    print("  [1] 查看当前会话   [2] 注入 Token   [3] 清除本地会话   [0] 返回")
    choice = ask("选择", "1")
    if choice == "1":
        return run("login_cli")
    if choice == "2":
        token = ask("token")
        satoken = ask("satoken（可空）")
        if not token:
            print(paint("  已取消", "yellow"))
            return pause()
        args = ["login_cli", "--token", token]
        if satoken:
            args += ["--satoken", satoken]
        return run(*args)
    if choice == "3":
        return run("login_cli", "--clear")
    return None


def act_run():
    print()
    print("  [1] 阳光跑（sun）   [2] 自由跑（free）   [0] 返回")
    choice = ask("选择", "2")
    if choice not in ("1", "2"):
        return None
    rtype = "sun" if choice == "1" else "free"
    distance = ask("目标里程(米)", "2000")
    lat = ask("起跑纬度", "0.0")
    lng = ask("起跑经度", "0.0")
    args = ["run_cli", "-t", rtype, "-d", distance, "--lat", lat, "--lng", lng]
    if rtype == "sun":
        plan = ask("长跑方案编号（留空自动拉取第一个）")
        if plan:
            args += ["--plan-code", plan]
    run(*args)


def act_score():
    print()
    print("  [1] 长跑成绩与规则   [2] 体测成绩   [3] 考试   [4] 考勤   [5] 全部")
    table = {"1": "run", "2": "fitness", "3": "exam", "4": "attend", "5": "all"}
    choice = ask("选择", "1")
    if choice not in table:
        return None
    run("score_cli", "-t", table[choice])


def act_history():
    print()
    print("  [1] 阳光跑历史   [2] 自由跑历史   [3] 全部")
    table = {"1": "sun", "2": "free", "3": "all"}
    choice = ask("选择", "1")
    if choice not in table:
        return None
    args = ["history_cli", "-t", table[choice]]
    detail = ask("查看指定跑单详情编号（留空跳过）")
    if detail:
        args += ["--detail", detail]
    run(*args)


def act_task():
    print()
    print("  [1] 每日任务列表   [2] 领取登录奖励   [3] 申诉记录   [4] 福利大厅")
    table = {"1": ["--tasks"], "2": ["--claim-login"], "3": ["--appeals"], "4": ["--jackpot"]}
    choice = ask("选择", "1")
    if choice not in table:
        return None
    run("task_cli", *table[choice])


def act_face():
    print()
    print("  [1] 查询人脸认证状态   [2] 拉取 OSS 上传令牌   [0] 返回")
    choice = ask("选择", "1")
    if choice == "1":
        return run("face_cli")
    if choice == "2":
        return run("face_cli", "--token-oss")
    return None


def act_ai():
    print()
    print("  动作: 1=深蹲 2=开合跳 3=跳绳 4=高抬腿 5=俯卧撑 6=平板支撑 ...")
    sport = ask("动作编号", "2")
    count = ask("次数", "35")
    duration = ask("用时(秒)", "62")
    run("ai_cli", "-s", sport, "-c", count, "-d", duration)


def act_community():
    print()
    print("  [1] 动态列表   [2] 热门频道   [3] 分类专区   [4] 搜索关键词")
    table = {"1": [], "2": ["--channels"], "3": ["--categories"]}
    choice = ask("选择", "1")
    if choice == "4":
        keyword = ask("关键词")
        if not keyword:
            return None
        return run("community_cli", "--search", keyword)
    if choice not in table:
        return None
    run("community_cli", *table[choice])


def act_alumni():
    print()
    print("  [1] 校友推荐   [2] 年级标签配置   [0] 返回")
    choice = ask("选择", "1")
    if choice == "1":
        return run("alumni_cli", "--schoolmates")
    if choice == "2":
        return run("alumni_cli", "--config")
    return None


def act_message():
    print()
    print("  [1] 评论消息   [2] 点赞消息   [3] 粉丝消息   [4] 系统信箱   [5] 好友通讯录")
    table = {"1": ["--comments"], "2": ["--favors"], "3": ["--fans"], "4": ["--box"], "5": ["--buddies"]}
    choice = ask("选择", "4")
    if choice not in table:
        return None
    run("message_cli", *table[choice])


def act_backend():
    deploy_dir = os.environ.get("SHANDONG_DEPLOY_DIR", "")
    script = os.path.join(deploy_dir, "check_backend.py") if deploy_dir else ""
    print()
    if script and os.path.exists(script):
        print(paint("  ── 后端连通性检测 ──────────────────────────────────", "dim"))
        code = subprocess.call([sys.executable, "-W", "ignore", script, "--target", HERE], cwd=HERE,
                               env={**os.environ, "PYTHONWARNINGS": "ignore"})
    else:
        print(paint("  未找到 deploy/check_backend.py，改用内置探测 ...", "dim"))
        code = subprocess.call(
            [sys.executable, "-W", "ignore", "-c",
             "import requests;r=requests.get('https://api.huachenjie.com/run-front/',timeout=10,verify=False);"
             "print('[状态] 后端连通 ... 成功');"
             "print('[字段] HTTP状态=%s 耗时=%.2fs'%(r.status_code,r.elapsed.total_seconds()))"],
            cwd=HERE, env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONWARNINGS": "ignore"})
    print()
    print(paint("  [完成] 退出码 0" if code == 0 else f"  [注意] 退出码 {code}", "green" if code == 0 else "yellow"))
    pause()


def act_help_all():
    clis = [
        "login_cli", "user_cli", "policy_cli", "score_cli", "rank_cli", "history_cli",
        "run_cli", "ai_cli", "face_cli", "task_cli", "community_cli", "alumni_cli", "message_cli",
    ]
    clear()
    print()
    print(paint("  全部工具（直接双击 run.cmd 或在此菜单调用）", "bold"))
    print()
    for cli in clis:
        code = subprocess.run([sys.executable, os.path.join(HERE, cli + ".py"), "--help"],
                              cwd=HERE, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        first = (code.stdout or "").strip().splitlines()
        usage = next((l for l in first if l.startswith("usage")), "")
        desc = ""
        for line in first:
            if line.strip() and not line.startswith("usage") and not line.startswith(" "):
                desc = line.strip()
                break
        print(paint(f"  {cli}.py", "green") + paint("  " + desc, "dim"))
        print(paint("    " + usage.replace("usage: ", ""), "dim"))
        print()
    pause()


MENU = [
    ("1", "冒烟自检（离线，验证代码完整性）", lambda: run("smoke_test_877")),
    ("2", "账号登录", act_login),
    ("3", "凭证管理（查看 / 注入 / 清除）", act_credential),
    ("4", "我的资料与学生卡", lambda: run("user_cli")),
    ("5", "跑步业务（阳光跑 / 自由跑）", act_run),
    ("6", "跑步历史与跑单详情", act_history),
    ("7", "成绩 / 体测 / 考试 / 考勤", act_score),
    ("8", "排行榜", lambda: run("rank_cli")),
    ("9", "每日任务 / 福利大厅", act_task),
    ("10", "人脸认证 / OSS 令牌", act_face),
    ("11", "AI 室内运动结算", act_ai),
    ("12", "社区动态", act_community),
    ("13", "校友专区", act_alumni),
    ("14", "消息中心", act_message),
    ("15", "跑步方案与电子围栏", lambda: run("policy_cli")),
    ("16", "后端连通性检测", act_backend),
    ("17", "全部工具帮助", act_help_all),
    ("0", "退出", None),
]


def banner():
    _, state = session_state()
    print()
    print(paint("  ╔══════════════════════════════════════════════════════════╗", "blue"))
    print(paint("  ║", "blue") + paint("   闪动校园 8.7.7  协议客户端 · 一键启动控制台          ", "bold") + paint("║", "blue"))
    print(paint("  ╚══════════════════════════════════════════════════════════╝", "blue"))
    print(paint("   目录: ", "dim") + HERE)
    print(paint("   解释器: ", "dim") + sys.executable)
    ok = "已登录" in state
    print(paint("   会话: ", "dim") + paint(state, "green" if ok else "yellow"))
    print()


def main():
    while True:
        clear()
        banner()
        for key, text, _ in MENU:
            if key == "0":
                continue
            print("   " + paint(f"[{key}]".rjust(5), "green") + "  " + text)
        print("   " + paint("[0]".rjust(5), "red") + "  退出")
        print()
        try:
            choice = input(paint("  请输入序号: ", "bold")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if choice == "0" or choice.lower() in ("q", "quit", "exit"):
            print(paint("\n  再见。\n", "dim"))
            return 0

        action = next((fn for key, _, fn in MENU if key == choice), "invalid")
        if action == "invalid":
            print(paint("  无效序号，请重新输入", "red"))
            try:
                input(paint("  按回车继续 ...", "dim"))
            except (EOFError, KeyboardInterrupt):
                return 0
            continue
        if action:
            try:
                action()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0


if __name__ == "__main__":
    sys.exit(main())
