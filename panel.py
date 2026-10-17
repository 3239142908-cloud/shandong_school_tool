"""闪动校园 8.7.7 — 图形启动面板。

双击「启动面板.cmd」运行。只用标准库 tkinter，不额外装依赖。

功能：
  登录       手机号 + 密码 → 自动过滑块 → 自动写入 session.json
  风控自检   时间窗 / 频率 / 间隔 / IP（结果打到日志区）
  自由跑     生成轨迹并提交（可选 dry-run / 只读探测）
  阳光跑     生成轨迹并提交（需要方案编号与围栏编号）
  状态栏     实时显示登录状态、设备、解释器
"""
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
SESSION = os.path.join(HERE, "session.json")
PY = sys.executable


# --------------------------------------------------------------------------
# 会话状态
# --------------------------------------------------------------------------
def read_session():
    try:
        with open(SESSION, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:
        return {}


def session_summary():
    d = read_session()
    tok = str(d.get("token") or "")
    if not tok or tok.startswith("<") or tok.startswith("YOUR_"):
        return "未登录", False
    uid = d.get("userId") or "?"
    sch = d.get("userName") or d.get("schoolCode") or "?"
    return f"已登录 · userId={uid} · {sch}", True


# --------------------------------------------------------------------------
# 子进程执行（异步 + 日志流）
# --------------------------------------------------------------------------
class Runner:
    def __init__(self, on_line, on_done):
        self.on_line = on_line
        self.on_done = on_done
        self.proc = None
        self.thread = None

    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, args, env_extra=None):
        if self.busy():
            self.on_line("[!] 已有任务在跑，等它结束\n")
            return
        self.thread = threading.Thread(target=self._run, args=(args, env_extra), daemon=True)
        self.thread.start()

    def _run(self, args, env_extra):
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
        vendor = os.path.join(HERE, "vendor")
        if os.path.isdir(vendor):
            env["PYTHONPATH"] = vendor + os.pathsep + env.get("PYTHONPATH", "")
        if env_extra:
            env.update(env_extra)
        self.on_line(f"$ {' '.join(str(a) for a in args)}\n")
        try:
            self.proc = subprocess.Popen(
                [PY, *args], cwd=HERE, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
            for line in self.proc.stdout:
                self.on_line(line)
            rc = self.proc.wait()
            self.on_line(f"\n[完成] exit={rc}\n{'=' * 60}\n")
            self.on_done(rc)
        except Exception as e:
            self.on_line(f"\n[错误] {type(e).__name__}: {e}\n")
            self.on_done(-1)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.on_line("\n[已请求终止]\n")
            except Exception:
                pass


# --------------------------------------------------------------------------
# 面板
# --------------------------------------------------------------------------
class Panel(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("闪动校园 8.7.7 · 启动面板")
        self.geometry("820x680")
        self.minsize(720, 600)
        self.q = queue.Queue()
        self.runner = Runner(self._enqueue, self._done)
        self._fences = []
        self._plans = []
        self._pending_autolocate = False
        self._build()
        self._center()
        self.after(80, self._drain)
        self.refresh_status()

    def _center(self):
        """把窗口放到屏幕正中，并短暂置顶 —— 避免"双击后什么都没看见"。

        有些环境下窗口默认落在屏幕外、或被其它窗口挡住，用户会以为程序没启动。
        """
        try:
            self.update_idletasks()
            w, h = 820, 680
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
            x = max(0, (sw - w) // 2)
            y = max(0, (sh - h) // 3)
            self.geometry(f"{w}x{h}+{x}+{y}")
            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)
            self.after(1200, lambda: self.attributes("-topmost", False))
            self.focus_force()
        except Exception:
            pass

    # ---- UI ----
    def _build(self):
        pad = {"padx": 8, "pady": 4}
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except Exception:
            pass

        # 标题
        head = ttk.Frame(self)
        head.pack(fill="x", **pad)
        ttk.Label(head, text="闪动校园 8.7.7 · 启动面板",
                  font=("Microsoft YaHei UI", 13, "bold")).pack(side="left")
        self.lbl_status = ttk.Label(head, text="…", foreground="#666")
        self.lbl_status.pack(side="right")

        # 登录
        box1 = ttk.LabelFrame(self, text=" 登录（手机号 + 密码，滑块全自动） ")
        box1.pack(fill="x", **pad)
        row = ttk.Frame(box1)
        row.pack(fill="x", padx=8, pady=6)
        ttk.Label(row, text="手机号").pack(side="left")
        self.e_mobile = ttk.Entry(row, width=18)
        self.e_mobile.pack(side="left", padx=(4, 12))
        ttk.Label(row, text="密码").pack(side="left")
        self.e_pass = ttk.Entry(row, width=18, show="*")
        self.e_pass.pack(side="left", padx=(4, 12))
        self.btn_login = ttk.Button(row, text="登录", command=self.do_login)
        self.btn_login.pack(side="left")
        ttk.Button(row, text="检查会话", command=self.do_probe).pack(side="left", padx=6)
        ttk.Button(row, text="清除会话", command=self.do_clear).pack(side="left")

        # 参数
        box2 = ttk.LabelFrame(self, text=" 跑步参数 ")
        box2.pack(fill="x", **pad)
        r1 = ttk.Frame(box2)
        r1.pack(fill="x", padx=8, pady=6)
        ttk.Label(r1, text="距离(米)").pack(side="left")
        self.e_dist = ttk.Entry(r1, width=8)
        self.e_dist.insert(0, "2000")
        self.e_dist.pack(side="left", padx=(4, 12))
        ttk.Label(r1, text="配速(秒/公里)").pack(side="left")
        self.e_pace = ttk.Entry(r1, width=8)
        self.e_pace.insert(0, "360")
        self.e_pace.pack(side="left", padx=(4, 12))
        ttk.Label(r1, text="纬度").pack(side="left")
        self.e_lat = ttk.Entry(r1, width=12)
        self.e_lat.pack(side="left", padx=(4, 12))
        ttk.Label(r1, text="经度").pack(side="left")
        self.e_lng = ttk.Entry(r1, width=12)
        self.e_lng.pack(side="left", padx=(4, 0))

        r2 = ttk.Frame(box2)
        r2.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(r2, text="方案编号").pack(side="left")
        self.e_plan = ttk.Entry(r2, width=22)
        self.e_plan.pack(side="left", padx=(4, 12))
        ttk.Label(r2, text="围栏编号").pack(side="left")
        self.e_fence = ttk.Entry(r2, width=22)
        self.e_fence.pack(side="left", padx=(4, 12))
        self.v_realtime = tk.BooleanVar(value=True)
        ttk.Checkbutton(r2, text="按真实时长等待", variable=self.v_realtime).pack(side="left")

        # 自动定位（登录后点一下，方案/围栏/坐标/距离全部自动填）
        r3 = ttk.Frame(box2)
        r3.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(r3, text="⟳ 自动定位到任务区域",
                   command=self.do_autolocate).pack(side="left")
        ttk.Label(r3, text="操场").pack(side="left", padx=(12, 4))
        self.cb_fence = ttk.Combobox(r3, width=18, state="readonly")
        self.cb_fence.pack(side="left")
        self.cb_fence.bind("<<ComboboxSelected>>", self._on_fence_pick)
        ttk.Label(r3, text="方案").pack(side="left", padx=(12, 4))
        self.cb_plan = ttk.Combobox(r3, width=18, state="readonly")
        self.cb_plan.pack(side="left")
        self.cb_plan.bind("<<ComboboxSelected>>", self._on_plan_pick)

        # 操作
        box3 = ttk.LabelFrame(self, text=" 操作 ")
        box3.pack(fill="x", **pad)
        r4 = ttk.Frame(box3)
        r4.pack(fill="x", padx=8, pady=8)
        ttk.Button(r4, text="① 风控自检", command=self.do_preflight).pack(side="left", padx=3)
        ttk.Button(r4, text="② 只读探测", command=self.do_probe).pack(side="left", padx=3)
        ttk.Button(r4, text="③ 自由跑(dry)", command=lambda: self.do_run("free", True)).pack(side="left", padx=3)
        ttk.Button(r4, text="④ 自由跑(提交)", command=lambda: self.do_run("free", False)).pack(side="left", padx=3)
        ttk.Button(r4, text="⑤ 阳光跑(dry)", command=lambda: self.do_run("sun", True)).pack(side="left", padx=3)
        ttk.Button(r4, text="⑥ 阳光跑(提交)", command=lambda: self.do_run("sun", False)).pack(side="left", padx=3)
        ttk.Button(r4, text="停止", command=self.do_stop).pack(side="right", padx=3)
        ttk.Button(r4, text="清空日志", command=self.clear_log).pack(side="right", padx=3)

        # 日志
        box4 = ttk.LabelFrame(self, text=" 日志 ")
        box4.pack(fill="both", expand=True, **pad)
        self.log = scrolledtext.ScrolledText(
            box4, wrap="word", font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4",
            insertbackground="#d4d4d4")
        self.log.pack(fill="both", expand=True, padx=6, pady=6)

    # ---- 自动定位 ----
    def do_autolocate(self):
        """登录后点一下：拉方案 + 围栏，自动填坐标/编号/距离。

        全部走只读接口（run/plan/selectList、querySchoolFences），不产生跑单。
        """
        self._enqueue("\n[自动定位] 拉取方案与围栏 …\n")

        def worker():
            try:
                from client import ShanDongClient
                c = ShanDongClient()
                if not c.transport.token:
                    self._enqueue("[自动定位] 未登录，先登录\n")
                    return
                fences, plans, cfg = [], [], {}
                # 1) 跑步方案
                try:
                    p = c.get_run_plan_list()
                    plans = (p.get("data") or {}).get("list") or []
                    self._enqueue(f"[自动定位] 方案 {len(plans)} 个\n")
                    for pl in plans:
                        self._enqueue(f"    {pl.get('runPlanCode')}  {pl.get('runPlanName')}\n")
                except Exception as e:
                    self._enqueue(f"[自动定位] 方案拉取失败: {e}\n")
                # 2) 电子围栏
                try:
                    f = c.query_school_fences()
                    fences = f.get("data") or []
                    self._enqueue(f"[自动定位] 围栏 {len(fences)} 个\n")
                    for fe in fences:
                        self._enqueue(
                            f"    {fe.get('fenceCode')}  {fe.get('fenceName')}  "
                            f"({fe.get('lat')}, {fe.get('lng')})\n")
                except Exception as e:
                    self._enqueue(f"[自动定位] 围栏拉取失败: {e}\n")
                # 3) 方案额外配置（目标里程要求）
                try:
                    if plans and fences:
                        cfg = c.check_sun_run_config(
                            run_plan_code=plans[0].get("runPlanCode", ""),
                            fence_code=fences[0].get("fenceCode", ""),
                            target_distance="2000") or {}
                        data = cfg.get("data") or {}
                        if data:
                            self._enqueue(f"[自动定位] 配置: {json.dumps(data, ensure_ascii=False)[:220]}\n")
                except Exception as e:
                    self._enqueue(f"[自动定位] 配置查询跳过: {e}\n")

                self._enqueue(("[locate]", fences, plans))
            except Exception as e:
                self._enqueue(f"[自动定位] 失败: {type(e).__name__}: {e}\n")

        threading.Thread(target=worker, daemon=True).start()

    def _apply_locate(self, fences, plans):
        """把拉到的数据填进控件。"""
        self._fences = fences
        self._plans = plans
        if fences:
            names = [f"{fe.get('fenceName')} ({fe.get('fenceCode')})" for fe in fences]
            self.cb_fence["values"] = names
            self.cb_fence.current(0)
            self._on_fence_pick()
        if plans:
            names = [f"{pl.get('runPlanName')} ({pl.get('runPlanCode')})" for pl in plans]
            self.cb_plan["values"] = names
            self.cb_plan.current(0)
            self._on_plan_pick()
        self._enqueue("[自动定位] 完成，坐标与编号已填入\n")

    def _on_fence_pick(self, _evt=None):
        idx = self.cb_fence.current()
        fences = getattr(self, "_fences", [])
        if 0 <= idx < len(fences):
            fe = fences[idx]
            lat, lng = fe.get("lat"), fe.get("lng")
            if lat is not None and lng is not None:
                self.e_lat.delete(0, "end")
                self.e_lat.insert(0, f"{float(lat):.6f}")
                self.e_lng.delete(0, "end")
                self.e_lng.insert(0, f"{float(lng):.6f}")
            fc = fe.get("fenceCode") or ""
            if fc:
                self.e_fence.delete(0, "end")
                self.e_fence.insert(0, fc)
            self._enqueue(f"[定位] 已选操场 {fe.get('fenceName')} → {lat}, {lng}\n")

    def _on_plan_pick(self, _evt=None):
        idx = self.cb_plan.current()
        plans = getattr(self, "_plans", [])
        if 0 <= idx < len(plans):
            pc = plans[idx].get("runPlanCode") or ""
            if pc:
                self.e_plan.delete(0, "end")
                self.e_plan.insert(0, pc)
            self._enqueue(f"[定位] 已选方案 {plans[idx].get('runPlanName')}\n")

    # ---- 日志 ----
    def _enqueue(self, text):
        self.q.put(text)

    def _drain(self):
        try:
            while True:
                item = self.q.get_nowait()
                # 后台线程不能直接碰 tkinter（after/lift 都会抛
                # "main thread is not in main loop"），所以结果走队列，
                # 由主线程在这里落地。
                if isinstance(item, tuple) and item and item[0] == "[locate]":
                    self._apply_locate(item[1], item[2])
                else:
                    self.log.insert("end", item)
                    self.log.see("end")
        except queue.Empty:
            pass
        self.after(80, self._drain)

    def clear_log(self):
        self.log.delete("1.0", "end")

    def _done(self, rc):
        self.after(0, self.refresh_status)
        # 登录成功后自动定位到任务区域
        if getattr(self, "_pending_autolocate", False):
            self._pending_autolocate = False
            if rc == 0:
                self.after(400, self.do_autolocate)

    # ---- 状态 ----
    def refresh_status(self):
        txt, ok = session_summary()
        self.lbl_status.config(text=txt, foreground="#0a7d28" if ok else "#b33")

    # ---- 动作 ----
    def _common(self):
        lat = self.e_lat.get().strip()
        lng = self.e_lng.get().strip()
        dist = self.e_dist.get().strip() or "2000"
        pace = self.e_pace.get().strip() or "360"
        return lat, lng, dist, pace

    def do_login(self):
        mob = self.e_mobile.get().strip()
        pwd = self.e_pass.get()
        if not mob or not pwd:
            messagebox.showwarning("提示", "请输入手机号和密码")
            return
        self._pending_autolocate = True
        self.runner.start(["login_cli.py", "-u", mob, "-p", pwd])

    def do_probe(self):
        self.runner.start(["run_cli.py", "-t", "free", "--stop-after", "probe"])

    def do_clear(self):
        if messagebox.askyesno("确认", "清除本地 session.json 里的凭证？（设备身份会保留）"):
            self.runner.start(["login_cli.py", "--clear"])

    def do_preflight(self):
        self.runner.start(["preflight.py", "sun"])

    def do_run(self, kind, dry):
        lat, lng, dist, pace = self._common()
        if not dry and (not lat or not lng):
            messagebox.showwarning("提示", "真实提交需要填起跑纬度和经度")
            return
        args = ["run_cli.py", "-t", kind, "-d", dist, "-p", pace]
        if lat and lng:
            args += ["--lat", lat, "--lng", lng]
        if dry:
            args += ["--dry-run"]
        else:
            args += ["--stop-after", "full"]
            if self.v_realtime.get():
                args += ["--realtime"]
            if kind == "sun":
                plan = self.e_plan.get().strip()
                fence = self.e_fence.get().strip()
                if plan:
                    args += ["--plan-code", plan]
                if fence:
                    args += ["--fence-code", fence]
        self.runner.start(args)

    def do_stop(self):
        self.runner.stop()


def main():
    """启动 GUI。任何异常都写日志并弹窗，避免 pythonw 下"打开一片空白"。"""
    log_path = os.path.join(HERE, "panel_error.log")
    try:
        app = Panel()
        app.mainloop()
        return 0
    except Exception:
        import traceback
        tb = traceback.format_exc()
        try:
            with open(log_path, "w", encoding="utf-8") as fh:
                fh.write(tb)
        except Exception:
            pass
        # 尽量弹窗告诉用户发生了什么（tkinter 本身可能已坏，所以再兜一层）
        try:
            import tkinter.messagebox as mb
            mb.showerror(
                "启动失败",
                f"面板启动出错，详情已写入：\n{log_path}\n\n{tb[:900]}")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
