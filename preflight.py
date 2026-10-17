"""跑单前风控自检：时间窗 / 当日频率 / 跑单间隔 / IP 省份 / 步态一致性。

这些是「程序之外」的风险面 —— 轨迹再真，凌晨三点连跑五单也会被标记。

工具是通用的：时间窗、期望省份/城市都从配置读取，不内置任何具体学校。
"""
import datetime
import json
import os
import sys
from typing import Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("SHANDONG_SESSION_FILE", os.path.join(HERE, "session.json"))


# 允许跑步的时间窗（默认值，可被 session.json 覆盖）
# 阳光跑的真实时段由服务端 checkSunRunConfig 的 alertTip 下发，
# 这里只是跑前自检的兜底；使用者可在 session.json 里改。
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


def expected_region_from_session() -> Tuple[str, str, bool, float, float, float]:
    """从 session.json 读使用者配置的跑步地信息。

    工具是通用的：每个人学校所在省市、是否禁止连 WiFi 都不同，不能写死。
      expectedProvince / expectedCity   留空则跳过省市判断
      requireMobileNetwork              true 时固网 IP 直接阻断；默认只提示
      expectedLat / expectedLng         填了就用「IP 归属地到跑步点的距离」判定
      maxIpDistanceKm                   距离阈值，默认 50 km
    """
    p = os.environ.get("SHANDONG_SESSION_FILE") or os.path.join(HERE, "session.json")
    try:
        with open(p, "r", encoding="utf-8-sig") as fh:
            d = json.load(fh)
        def _f(v, dflt=0.0):
            try:
                return float(v)
            except Exception:
                return dflt
        return (str(d.get("expectedProvince") or "").strip(),
                str(d.get("expectedCity") or "").strip(),
                bool(d.get("requireMobileNetwork", False)),
                _f(d.get("expectedLat"), 0.0),
                _f(d.get("expectedLng"), 0.0),
                _f(d.get("maxIpDistanceKm"), 50.0))
    except Exception:
        return "", "", False, 0.0, 0.0, 50.0


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """两点球面距离（公里）。"""
    import math
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(max(0.0, min(1.0, a))))


def check_ip(expect_province: str = "", expect_city: str = "",
             require_mobile: bool = False,
             expect_lat: float = 0.0, expect_lng: float = 0.0,
             max_km: float = 50.0):
    """出口 IP 归属地与「使用者配置的跑步地」是否一致。

    三级判定，从精确到粗略：
      1. 配了 expectedLat/expectedLng → 直接算 IP 归属地到跑步点的距离
      2. 只配了 expectedProvince      → 比省份（城市不同算「同省异地」）
      3. 什么都没配                   → 只报告归属地，不阻断

    国内库优先（淘宝 → 太平洋），国际库只作兜底：国内 IP 在国际库里
    普遍只精确到省，会把同省不同城市的出口混在一起。
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

        # 3) 一致性判断
        # 3a) 最高精度：配了跑步点坐标，就直接算「IP 归属地 ↔ 跑步点」距离
        ip_lat = ip_lng = None
        try:
            jc = _http_json(f"http://ip-api.com/json/{ip}?fields=lat,lon")
            if jc.get("lat") is not None:
                ip_lat, ip_lng = float(jc["lat"]), float(jc["lon"])
        except Exception:
            pass

        dist_km = None
        if expect_lat and expect_lng and ip_lat is not None and ip_lng is not None:
            dist_km = _haversine_km(ip_lat, ip_lng, expect_lat, expect_lng)

        if dist_km is not None:
            base = (f"IP {ip} → {region}{city} {isp}；"
                    f"距跑步点 {dist_km:.0f} km（阈值 {max_km:.0f}）")
            if dist_km > max_km:
                return False, base + "  **超出阈值，IP 与跑步地不一致**"
            msg = base + "  ✓"
        elif not expect_province:
            msg = (f"IP {ip} → {region}{city} {isp}"
                   "（未配置 expectedProvince / expectedLat，跳过一致性判断）")
            # 未配置时固网只提示、不阻断：自由跑本来就没有「不许连 WiFi」的要求，
            # 不该因为没填配置就把所有跑单都拦掉。
            try:
                j2 = _http_json(f"http://ip-api.com/json/{ip}?fields=mobile")
                if j2.get("mobile") is False:
                    msg += "；固网 IP（若要求跑步不得连 WiFi，请改用手机热点/USB 共享）"
                elif j2.get("mobile") is True:
                    msg += "；移动网络 ✓"
            except Exception:
                pass
            return True, msg
        else:
            prov_hit = (expect_province in region) or (region in expect_province) \
                       or (expect_province.lower() in region.lower())
            if not prov_hit:
                return False, (f"出口 IP {ip} 归属 {region}{city}，"
                               f"与配置的「{expect_province}{expect_city}」**跨省**")
            if expect_city:
                same_city = (expect_city in city) or (city in expect_city)
                msg = (f"IP OK（{ip} → {region}{city} {isp}，"
                       f"{'同城' if same_city else f'同省异地（{city}），可接受'}）")
            else:
                msg = f"IP OK（{ip} → {region}{city} {isp}，同省）"

        # 4) 固网/移动判定
        try:
            j2 = _http_json(f"http://ip-api.com/json/{ip}?fields=mobile")
            if j2.get("mobile") is False:
                tip = ("；该 IP 为**固网**，若学校要求跑步不得连 WiFi，"
                       "请改用手机热点/USB 共享走 4G，并把 session.json 的 "
                       "requireMobileNetwork 设为 true")
                if require_mobile:
                    return False, msg + tip
                return True, msg + tip
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

    IP 一致性用 session.json 里的 expectedProvince / expectedCity，
    工具不内置任何具体校区，使用者填什么就按什么判。
    """
    prov, city, req_mobile, elat, elng, max_km = expected_region_from_session()
    lines = []
    ok_all = True
    for fn in (lambda: check_time(run_type, now),
               lambda: (check_daily(client, run_type, now)[:2]),
               lambda: check_ip(prov, city, req_mobile, elat, elng, max_km)):
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
