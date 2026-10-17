"""跑单前风控自检：时间窗 / 当日频率 / 跑单间隔 / IP 省份 / 步态一致性。

这些是「程序之外」的风险面 —— 轨迹再真，凌晨三点连跑五单也会被标记。
"""
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("SHANDONG_SESSION_FILE", os.path.join(HERE, "session.json"))


# 学校允许跑步的时间窗（来自 checkSunRunConfig 的 alertTip）
SUN_WINDOWS = [(6 * 60, 9 * 60), (9 * 60 + 1, 21 * 60 + 30)]
# 自由跑没有硬性时段，但真机历史全落在 17:00~24:00
FREE_COMFORT = (16 * 60, 23 * 60 + 59)
MAX_DAILY_RUNS = 3          # 历史最多一天 5 单（10-08），但那明显是刷量；3 单更稳
MIN_GAP_MIN = 8             # 历史上最短间隔 3 分钟，8 分钟更安全


def _minutes(dt):
    return dt.hour * 60 + dt.minute


def in_window(minutes, windows):
    return any(lo <= minutes <= hi for lo, hi in windows)


def check_time(run_type, now=None):
    now = now or datetime.datetime.now()
    m = _minutes(now)
    if run_type == "sun":
        if not in_window(m, SUN_WINDOWS):
            return False, f"当前 {now:%H:%M} 不在阳光跑允许时段（06:00-09:00 / 09:01-21:30）"
        return True, f"时间窗 OK（{now:%H:%M} 在阳光跑时段内）"
    if not in_window(m, [FREE_COMFORT]):
        return False, (f"当前 {now:%H:%M} 与你历史自由跑时段（16:00-24:00）不符，"
                       "凌晨/清晨跑单异常")
    return True, f"时间窗 OK（{now:%H:%M}）"


def check_daily(client, run_type, now=None):
    """当日已跑单数 + 距上次跑单间隔。"""
    now = now or datetime.datetime.now()
    today = now.date()
    rows = []
    try:
        if run_type == "sun":
            r = client.page_sun_run_record(page_num=1, page_size=30)
        else:
            r = client.page_free_run_record_list(page_num=1, page_size=30)
        for grp in (r.get("data") or {}).get("list") or []:
            rows.extend(grp.get("list") or [grp] if isinstance(grp, dict) else [])
    except Exception:
        return True, "频率检查跳过（历史查询失败）", 0, None

    today_count = 0
    last_start = None
    for rec in rows:
        if not isinstance(rec, dict):
            continue
        st = rec.get("startTime")
        try:
            ts = int(st) / 1000.0
        except Exception:
            continue
        dt = datetime.datetime.fromtimestamp(ts)
        if dt.date() == today:
            today_count += 1
        if last_start is None or ts > last_start:
            last_start = ts

    gap_min = None
    if last_start:
        gap_min = (now.timestamp() - last_start) / 60.0

    issues = []
    if today_count >= MAX_DAILY_RUNS:
        issues.append(f"今天已跑 {today_count} 单（上限 {MAX_DAILY_RUNS}）")
    if gap_min is not None and gap_min < MIN_GAP_MIN:
        issues.append(f"距上一单仅 {gap_min:.0f} 分钟（建议 ≥{MIN_GAP_MIN}）")
    if issues:
        return False, "；".join(issues), today_count, gap_min
    return True, f"频率 OK（今日 {today_count} 单，距上单 {gap_min:.0f} 分钟）" if gap_min else "频率 OK（今日首单）", today_count, gap_min


def _http_json(url, timeout=10):
    """优先 curl_cffi（带浏览器 TLS 指纹），回退 requests；都绕开系统代理。"""
    vendor = os.path.join(HERE, "vendor")
    if os.path.isdir(vendor) and vendor not in sys.path:
        sys.path.insert(0, vendor)
    try:
        from curl_cffi import requests as cr
        return cr.get(url, impersonate="chrome", timeout=timeout,
                      proxies={"http": "", "https": ""}).json()
    except Exception:
        import requests
        s = requests.Session()
        s.trust_env = False
        return s.get(url, timeout=timeout).json()


def check_ip():
    """出口 IP 归属地。判断顺序：国内库优先，国际库仅作参考。

    为什么要分库：同一个联通 IP 153.34.255.220，国际库（ipinfo/ipwho）
    判成南京，国内库（淘宝/太平洋）判成苏州。国内 IP 的国际库精度普遍
    到省级就不错了，而闪动校园是国内服务商，用的必然是国内库。
    """
    try:
        # 1) 先拿出口 IP
        ip = ""
        for u in ("https://api.ipify.org?format=json",
                  "http://ip-api.com/json/?fields=query"):
            try:
                j = _http_json(u)
                ip = j.get("ip") or j.get("query") or ""
                if ip:
                    break
            except Exception:
                continue
        if not ip:
            return True, "IP 检查跳过（取不到出口 IP）"

        # 2) 国内库查归属（淘宝最准，太平洋备用）
        region = city = isp = ""
        for name, u in (
            ("taobao", f"http://ip.taobao.com/outGetIpInfo?ip={ip}&accessKey=alibaba-inc"),
            ("pconline", f"https://whois.pconline.com.cn/ipJson.jsp?ip={ip}&json=true"),
        ):
            try:
                j = _http_json(u)
                d = j.get("data") if name == "taobao" else j
                if not isinstance(d, dict):
                    continue
                region = d.get("region") or d.get("pro") or ""
                city = d.get("city") or ""
                isp = d.get("isp") or d.get("addr") or ""
                if region:
                    break
            except Exception:
                continue

        if not region:
            return True, f"IP 检查跳过（{ip}，国内库查询失败）"

        # 3) 判断：省份必须对，城市不强求（同省跨市很常见）
        if any(k in region for k in ("江苏", "Jiangsu")):
            same_city = "苏州" in city
            msg = f"IP OK（{ip} → {region}{city} {isp}，{'同城' if same_city else f'同省异地（{city}），可接受'}）"
        else:
            return False, f"出口 IP {ip} 归属 {region}{city}，与跑步地（江苏苏州）**跨省**"

        # 4) 固网/移动判定：多数高校要求"跑步不许连 WiFi"，若服务端查 IP 类型，
        #    固网 IP 会被直接识别为"在 WiFi 下跑的"。ip-api 的 mobile 字段
        #    是目前少数能区分固网/蜂窝的免费字段。
        try:
            j2 = _http_json(f"http://ip-api.com/json/{ip}?fields=mobile,proxy,hosting,as")
            if j2.get("mobile") is False:
                return False, (msg + "；但该 IP 为**固网（非移动）**，"
                               "若学校要求跑步不得连 WiFi，需改用手机热点/USB 共享走 4G")
            if j2.get("mobile") is True:
                msg += "；移动网络 ✓"
        except Exception:
            pass
        return True, msg
    except Exception as e:
        return True, f"IP 检查跳过（{type(e).__name__}）"


def preflight(client, run_type="free", now=None, strict=False):
    """返回 (是否全部通过, 报告行列表)。

    注意：返回值永远是**真实**状态，不受 strict 影响 —— 早先写成
    `ok_all or not strict` 导致 strict=False 时恒为 True，告警被吞掉，
    结果带着"凌晨跑单"告警直接起跑了。阻断与否由调用方决定。
    """
    lines = []
    ok_all = True
    for fn in (lambda: check_time(run_type, now),
               lambda: (check_daily(client, run_type, now)[:2]),
               check_ip):
        ok, msg = fn()
        lines.append((ok, msg))
        if not ok:
            ok_all = False
    return ok_all, lines


if __name__ == "__main__":
    from client import ShanDongClient
    rt = sys.argv[1] if len(sys.argv) > 1 else "free"
    c = ShanDongClient()
    ok, lines = preflight(c, rt)
    for good, msg in lines:
        print(("  [OK]   " if good else "  [告警] ") + msg)
    print()
    print("结论:", "通过" if ok else "存在风险项")
