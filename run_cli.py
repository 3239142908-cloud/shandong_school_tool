"""长跑业务接口调度工具 —— 起跑 / 轨迹上传 / 完赛结算全链路。

历史版本只演示到"发起起跑"，轨迹算法由 running_protocol.py 抛 NotImplementedError。
本版本接入 trajectory_engine，产出物理自洽、围栏内、可解释的完整跑单数据，
并按真机客户端的接口顺序与节奏分片上传。

用法：
    # 只看会提交什么，不发任何请求
    run.cmd run_cli --dry-run -t sun -d 2000 -p 360

    # 真实执行（需要有效 token）
    run.cmd run_cli -t sun -d 2000 -p 360 --fence fence.json

    # 自由跑
    run.cmd run_cli -t free -d 1600 -p 380

安全开关：
    --dry-run            只生成数据不联网（默认关闭）
    --no-pre-behavior    跳过跑前浏览行为注入
    --speed 1            上传节奏倍率（1=按真机间隔实时，>1 加速，仅调试用）
"""
import argparse
import json
import math
import os
import random
import sys
import time

cur_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, cur_dir)

import trajectory_engine as te
import preflight as pf
from client import ShanDongClient
from logger import log_status, log_fields, log_error, log_detail


# ---------------------------------------------------------------------------
# 围栏获取与解析
# ---------------------------------------------------------------------------

def load_fence_file(path):
    """从 JSON 文件读取围栏。支持 [[lat,lng],...] 或 {"points":[[lat,lng],...]}。"""
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    points = raw.get("points") if isinstance(raw, dict) else raw
    out = []
    for item in points or []:
        if isinstance(item, dict):
            lat = item.get("lat") or item.get("latitude")
            lng = item.get("lng") or item.get("longitude")
            if lat is not None and lng is not None:
                out.append((float(lat), float(lng)))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            out.append((float(item[0]), float(item[1])))
    return out


def extract_fence_from_response(res, near_lat=None, near_lng=None):
    """从 querySchoolFences 响应里提取围栏点位（平面 → [(lat,lng),...]）。

    实测 8.7.7 的真实结构：
        {"code":0,"data":[{ "fenceCode","fenceName","lat","lng",
                            "fenceList":[{"lng":..,"lat":..}, ...] }, ...]}
    学校通常有多个围栏（田径场/小操场/风雨操场），给了参考坐标就选最近的那个。
    """
    if not isinstance(res, dict) or res.get("code") != 0:
        return []
    data = res.get("data")
    candidates = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
    if not candidates:
        return []

    parsed = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        pts = []
        # 真实字段优先
        for key in ("fenceList", "fencePoints", "points", "boundary",
                    "fencePointList", "coordinateList", "polygon", "spatialData"):
            val = item.get(key)
            if not val:
                continue
            pts = _parse_points_blob(val)
            if pts:
                break
        if not pts:
            lngs = item.get("lngs") or item.get("longitudes")
            lats = item.get("lats") or item.get("latitudes")
            if lngs and lats:
                try:
                    xs = [float(v) for v in str(lngs).replace(";", ",").split(",") if v.strip()]
                    ys = [float(v) for v in str(lats).replace(";", ",").split(",") if v.strip()]
                    if len(xs) == len(ys) >= 3:
                        pts = list(zip(ys, xs))
                except Exception:
                    pass
        if pts:
            parsed.append({
                "code": item.get("fenceCode"),
                "name": item.get("fenceName"),
                "lat": item.get("lat"),
                "lng": item.get("lng"),
                "points": pts,
            })

    if not parsed:
        return []
    if near_lat and near_lng and len(parsed) > 1:
        def dist(f):
            if f.get("lat") is None or f.get("lng") is None:
                return float("inf")
            return (float(f["lat"]) - near_lat) ** 2 + (float(f["lng"]) - near_lng) ** 2
        parsed.sort(key=dist)
        log_fields(选用围栏=f"{parsed[0]['name']}（{parsed[0]['code']}）")
    elif len(parsed) > 1:
        log_fields(围栏数=len(parsed), 选用=f"{parsed[0]['name']}（{parsed[0]['code']}）")
    return parsed[0]["points"]


def _parse_points_blob(blob):
    """解析多种点位表示：JSON 数组 / "lng,lat;lng,lat" / 字典数组。"""
    if isinstance(blob, list):
        out = []
        for it in blob:
            if isinstance(it, dict):
                lat = it.get("lat") or it.get("latitude")
                lng = it.get("lng") or it.get("longitude")
                if lat is not None and lng is not None:
                    out.append((float(lat), float(lng)))
            elif isinstance(it, (list, tuple)) and len(it) >= 2:
                out.append((float(it[1]), float(it[0])))   # 上游多为 [lng, lat]
        return out
    if isinstance(blob, str):
        txt = blob.strip()
        if txt.startswith("[") or txt.startswith("{"):
            try:
                return _parse_points_blob(json.loads(txt))
            except Exception:
                return []
        out = []
        for seg in txt.replace("|", ";").split(";"):
            parts = [p for p in seg.replace(" ", "").split(",") if p]
            if len(parts) >= 2:
                try:
                    out.append((float(parts[1]), float(parts[0])))
                except Exception:
                    pass
        return out
    return []


# ---------------------------------------------------------------------------
# 行为注入
# ---------------------------------------------------------------------------

def pre_run_behavior(client, rng):
    """跑前浏览行为：真机用户进 App 不会直奔跑步模块。

    这不是"伪装"，是补齐行为链条：服务端若比对「起跑前 N 分钟的行为序列」，
    一个只有 startFreeRun 而没有首页/社区/排行榜痕迹的账号本身就是异常。

    顺序按真机真实路径走：首页 → 消息 → 社区（点开帖子）→ 排行榜 → 个人资料。
    每步之间加随机停顿，避免整串请求挤在几百毫秒内。
    """
    steps = []

    def gap(lo=0.8, hi=3.2):
        time.sleep(rng.uniform(lo, hi))

    try:
        # 1) 消息中心（真机进 App 第一件事常是看红点）
        steps.append(("查询消息红点", client.get_message_red_dot()))
        gap(0.6, 2.2)

        # 2) 社区推荐流 + 点开两个帖子 + 上报浏览
        works = client.get_works_recommend_pool(page_num=1, page_size=rng.choice([5, 10, 20]))
        steps.append(("浏览社区推荐流", works))
        if works.get("code") == 0 and isinstance(works.get("data"), dict):
            lst = works["data"].get("list") or []
            for w in lst[:rng.randint(1, 2)]:
                wn = (w or {}).get("worksNo")
                if not wn:
                    continue
                gap(1.5, 4.5)
                detail = client.get_works_detail(wn)
                steps.append(("查看帖子详情", detail))
                gap(2.0, 6.0)          # 阅读停留
                steps.append(("上报浏览", client.update_works_visit(wn)))
        gap(1.0, 3.0)

        # 3) 排行榜（真机跑者会看自己排名）
        try:
            steps.append(("里程排行榜", client.get_rank_distance()))
        except Exception:
            pass
        gap(0.8, 2.5)

        # 4) 个人资料 / 学生卡（顺手点一下）
        try:
            steps.append(("个人资料", client.get_personal_info()))
        except Exception:
            pass
        gap(0.6, 2.0)

        # 5) 学校跑步概览（进入跑步模块前的入口页）
        try:
            steps.append(("学校跑步概览", client.query_school_run_summary()))
        except Exception:
            pass

    except Exception as e:
        steps.append(("浏览行为异常", {"code": -1, "message": str(e)}))
    return steps


def keepalive(client, rng, rounds=2):
    """跑前保活：模拟「App 已经在前台待了一会儿」。

    真机用户不会一打开 App 就起跑 —— 会先看一眼消息、刷一下首页，中间
    隔几十秒到几分钟。我们如果从"账号完全静默"直接跳到 startRun，
    活跃度曲线是断的。
    """
    out = []
    for i in range(max(1, rounds)):
        try:
            out.append(("保活·消息红点", client.get_message_red_dot()))
        except Exception:
            pass
        if i + 1 < rounds:
            time.sleep(rng.uniform(12.0, 45.0))
    return out


def post_run_behavior(client, run_record_code, run_type, rng):
    """跑单后行为：真机结算完不会立刻退出 App。

    正常路径是：看到成绩页 → 点进详情看轨迹 → 回排行榜看排名 → 回首页。
    结算后 0 请求直接下线，等于"跑完就把手机关了"，是可识别的行为缺口。
    """
    steps = []
    sport_type = 1 if run_type == "sun" else 2

    def gap(lo, hi):
        time.sleep(rng.uniform(lo, hi))

    try:
        gap(2.5, 6.0)                      # 成绩页停留
        steps.append(("查看跑单详情",
                      client.run_record_detail(run_record_code, sport_type=sport_type)))
        gap(3.0, 9.0)                      # 看轨迹/配速
        try:
            steps.append(("查看排行榜", client.get_rank_distance()))
        except Exception:
            pass
        gap(2.0, 7.0)
        try:
            steps.append(("刷新消息红点", client.get_message_red_dot()))
        except Exception:
            pass
        gap(1.5, 5.0)
        try:
            steps.append(("刷新学校跑步概览", client.query_school_run_summary()))
        except Exception:
            pass
    except Exception as e:
        steps.append(("跑后行为异常", {"code": -1, "message": str(e)}))
    return steps


def upload_with_pacing(client, label, func, speed_ratio=1.0, rng=None):
    """带节奏停顿的接口调用。speed_ratio<1 会拉长间隔（更像真机）。"""
    rng = rng or random
    try:
        res = func()
    except Exception as e:
        return {"code": -1, "message": f"{label} 调用异常: {e}"}
    # 上传分片之间的自然间隔（原版为毫秒级连发，是明显的机器人特征）
    base = rng.uniform(0.6, 2.2)
    time.sleep(max(0.0, base / max(0.01, speed_ratio)))
    return res


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_sun(client, args, rng):
    plan_code = args.plan_code
    if not plan_code and not args.dry_run:
        p_res = client.get_run_plan_list()
        if p_res.get("code") == 0 and p_res.get("data", {}).get("list"):
            plan_code = p_res["data"]["list"][0].get("runPlanCode", "")
    log_fields(方案代码=plan_code or "未指定", 目标里程=f"{args.distance}m", 目标配速=f"{args.pace}s/km")

    # 起跑前预检：真机一定会走这两步，跳过会在服务端留下缺失的调用链
    if not args.dry_run:
        pf_ok, pf_lines = pf.preflight(client, "sun")
        for good, msg in pf_lines:
            log_status("风控自检", ok=good, status_text=msg)
        if not pf_ok and not getattr(args, "no_strict_preflight", False):
            log_error("风控自检未通过，已阻断（加 --no-strict-preflight 可强制继续）")
            return 1

        cfg = client.check_sun_run_config(
            run_plan_code=plan_code, fence_code=args.fence_code,
            target_distance=str(args.distance),
        )
        log_status("考核配置预检", ok=cfg.get("code") == 0)
        if cfg.get("code") != 0:
            log_detail(cfg)
            log_error("预检未通过，终止起跑（避免产生异常跑单）")
            return 1
        checkin = client.get_pre_run_check_in()
        log_status("起跑前打卡校验", ok=checkin.get("code") == 0)

    # 1) 起跑
    if args.dry_run:
        run_record_code = "DRYRUN_RECORD_CODE"
        log_status("发起阳光跑起跑", ok=True, status_text="dry-run 跳过")
    else:
        start_res = client.start_sun_run_v2(
            run_plan_code=plan_code,
            target_distance=str(args.distance),
            lng=f"{args.lng:.6f}", lat=f"{args.lat:.6f}",
        )
        if start_res.get("code") != 0:
            log_status("发起阳光跑起跑", ok=False)
            log_detail(start_res)
            return 1
        run_record_code = (start_res.get("data") or {}).get("runRecordCode")
        log_status("发起阳光跑起跑", ok=True)
        log_fields(跑单编号=run_record_code)

    # 2) 生成轨迹（阳光跑专用：非整数圈 / 步频104 / pois 7字段 / 3秒采样）
    fence = _resolve_fence(client, args)
    log_fields(围栏点数=len(fence), 跑法="sun")

    # 人脸打卡图：真机每次都是 192x256 的 SDK 输出小图（8KB 级），
    # 直接复用底图 URL 会在"尺寸/体积"和"复用底图"两处露馅。
    # 未显式指定 --face-img 时，按真机规格重编码底图并上传 OSS。
    face_img = args.face_img
    if not face_img and not args.dry_run:
        try:
            from face_prep import prepare_face_image
            log_status("准备人脸打卡图", ok=True, status_text="按真机规格重编码")
            face_img, finfo = prepare_face_image()
            if face_img:
                log_fields(人脸图=f"{finfo.get('out_size')} {finfo.get('bytes')}B")
            else:
                log_status("准备人脸打卡图", ok=False,
                           status_text=str((finfo or {}).get("error"))[:60])
        except Exception as e:
            log_status("准备人脸打卡图", ok=False, status_text=f"{type(e).__name__}: {e}")
    if not face_img and args.dry_run:
        # dry-run 不联网，用占位 URL 让 1 条 faceCheck 也出现在预览里
        face_img = ("https://oss-cdn.huachenjie.com/prod/run-front/face/run/DRYRUN.jpg")

    gen = te.generate_sun_trajectory(
        start_lat=args.lat,
        start_lng=args.lng,
        target_distance_m=float(args.distance),
        avg_pace_sec_per_km=float(args.pace),
        fence=fence,
        seed=args.seed,
        face_img_url=face_img or "",
    )
    log_status("生成轨迹数据", ok=True)
    log_fields(
        距离=f"{gen['distance']}m", 时长=f"{gen['duration']}s", 总步数=gen["totalStep"],
        平均配速=f"{gen['pace']}s/km", 采样点=len(gen["pois"]),
        步频分片=len(gen["stepList"]), 配速分片=len(gen["paceList"]),
        步幅分片=len(gen["strideList"]), 打卡点=len(gen["targetPoints"]),
        圈数=f"{gen['laps']}×{gen['lapLength']}m",
        轨迹范围=f"{gen['spanX']}×{gen['spanY']}m",
        人脸打卡=f"{len(gen['faceCheckInfoList'])}条",
    )

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({k: v for k, v in gen.items() if not k.endswith("Obj")},
                      fh, ensure_ascii=False, indent=2)
        log_status(f"写出 {args.out}", ok=True)

    if args.dry_run:
        log_status("上传与结算", ok=True, status_text="dry-run 跳过（数据已生成，未发请求）")
        return 0

    if args.stop_after == "start":
        log_status("停止于起跑", ok=True, status_text="已创建跑单但未上传/结算")
        log_fields(跑单编号=run_record_code)
        return 0

    # 3) 分片上传：顺序与真机一致（轨迹 -> 步频 -> 配速 -> 打卡 -> 步幅）
    up = upload_with_pacing(client, "轨迹上报",
                            lambda: client.upload_run_record(run_record_code, gen["pois"]),
                            args.speed, rng)
    log_status("轨迹上报 uploadRunRecord", ok=up.get("code") == 0)
    if up.get("code") != 0:
        log_detail(up)

    up = upload_with_pacing(client, "步频上报",
                            lambda: client.upload_steps_record(
                                run_record_code, gen["stepListObj"], step_interval=60),
                            args.speed, rng)
    log_status("步频上报 uploadStepsRecord", ok=up.get("code") == 0)
    if up.get("code") != 0:
        log_detail(up)

    up = upload_with_pacing(client, "配速上报",
                            lambda: client.upload_pace_record(
                                run_record_code, gen["paceListObj"], pace_interval=1000),
                            args.speed, rng)
    log_status("配速上报 uploadPaceRecord", ok=up.get("code") == 0)
    if up.get("code") != 0:
        log_detail(up)

    if gen["targetPoints"]:
        up = upload_with_pacing(client, "打卡点上报",
                                lambda: client.upload_pass_point(run_record_code, gen["targetPoints"]),
                                args.speed, rng)
        log_status("打卡点上报 uploadPassPoint", ok=up.get("code") == 0)
        if up.get("code") != 0:
            log_detail(up)

    up = upload_with_pacing(client, "步幅上报",
                            lambda: client.upload_stride_record(run_record_code, gen["strideList"]),
                            args.speed, rng)
    log_status("步幅上报 uploadStrideRecord", ok=up.get("code") == 0)
    if up.get("code") != 0:
        log_detail(up)

    # 4) 结算。本账号 faceAuth=true 时必须带 faceCheckRecordList，
    #    否则服务端在 800~1700m 区间要求的打卡记录为空，会被判无效。
    fin = client.finish_sun_run_v2(
        run_record_code=run_record_code,
        distance=gen["distance"],
        target_points=gen["targetPoints"],
        # 真机 stepList 按 60 秒分片（2006m/631s → 11 片），
        # 而该方法默认 step_interval=20（自由跑口径），不显式覆盖会让
        # 服务端按 20 秒重新聚合，分片数与真机不符。
        step_interval=60,
        run_img_record=None,
        pause_count=0,
        pause_times=0,
        duration=gen["duration"],
        face_check_record_list=gen["faceCheckInfoList"] or None,
    )
    log_status("完赛结算 finishSunRun_v2", ok=fin.get("code") == 0)
    if fin.get("code") == 0:
        log_detail(fin.get("data"))
        # 跑后行为：真机不会跑完直接下线
        for label, res in post_run_behavior(client, run_record_code, "sun", rng):
            log_status(label, ok=(res or {}).get("code") == 0)
    else:
        log_detail(fin)
        return 1
    return 0


def probe_only(client):
    """只读探测：不产生任何写入动作，用于确认凭证可用与接口契约。

    这几个接口都是打开 App 就会触发的只读查询，不创建跑单、不改状态、
    不进入风控的写入判定面。
    """
    ok_all = True

    info = client.query_common_user_info()
    ok = info.get("code") == 0
    ok_all = ok_all and ok
    log_status("凭证核验 queryCommonUserInfo", ok=ok)
    if ok and isinstance(info.get("data"), dict):
        d = info["data"]
        try:
            import crypto
            log_fields(
                姓名=crypto.decrypt_field(d.get("userName", "")) or "（未返回）",
                学校=crypto.decrypt_field(d.get("schoolName", "")) or "（未返回）",
                用户ID=d.get("userId") or "（未返回）",
            )
        except Exception:
            log_fields(用户ID=d.get("userId") or "（未返回）")
    else:
        log_detail(info)

    hist = client.page_free_run_record_list(page_num=1, page_size=5)
    ok = hist.get("code") == 0
    ok_all = ok_all and ok
    log_status("自由跑历史 pageFreeRunRecord", ok=ok)
    if ok and isinstance(hist.get("data"), dict):
        groups = hist["data"].get("list") or []
        # 真机返回是按月分组的嵌套结构：[{monthDate, totalTime, list:[{runRecordCode...}]}]
        rows = []
        for grp in groups:
            if isinstance(grp, dict) and isinstance(grp.get("list"), list):
                rows.extend(r for r in grp["list"] if isinstance(r, dict))
            elif isinstance(grp, dict) and grp.get("runRecordCode"):
                rows.append(grp)
        log_fields(历史条数=len(rows))
        coords = None
        for r in rows[:5]:
            log_fields(
                编号=r.get("runRecordCode"),
                距离=r.get("distance"),
                时长=r.get("duration"),
                步数=r.get("totalStep"),
                时间=r.get("formatTime") or r.get("startTime") or r.get("createTime"),
            )
            # 从历史跑单里捞轨迹起点坐标，供本次生成轨迹定位用
            if coords is None:
                for lk in ("lat", "latitude", "startLat"):
                    for gk in ("lng", "longitude", "startLng"):
                        if r.get(lk) not in (None, "", 0, "0") and r.get(gk) not in (None, "", 0, "0"):
                            try:
                                coords = (float(r[lk]), float(r[gk]))
                            except Exception:
                                coords = None
                            break
                    if coords:
                        break
        if coords:
            log_fields(历史起点坐标=f"{coords[0]:.6f},{coords[1]:.6f}",
                       用途="可作为本次 --lat/--lng 使用")
        else:
            log_fields(历史坐标="响应未含坐标字段，需手动提供 --lat/--lng")
    else:
        log_detail(hist)

    plans = client.get_run_plan_list()
    ok = plans.get("code") == 0
    log_status("跑步方案 run/plan/selectList", ok=ok)
    if ok and isinstance(plans.get("data"), dict):
        log_fields(方案数=len(plans["data"].get("list") or []))

    return 0 if ok_all else 1


def run_free(client, args, rng):
    log_fields(目标里程=f"{args.distance}m", 起点经度=args.lng, 起点纬度=args.lat)

    # 跑单前风控自检：时间窗 / 当日频率 / 跑单间隔 / 出口 IP 省份。
    # 这些是轨迹之外最容易翻车的面 —— 凌晨连跑几单，轨迹再真也会被标记。
    if not args.dry_run:
        pf_ok, pf_lines = pf.preflight(client, "free")
        for good, msg in pf_lines:
            log_status("风控自检", ok=good, status_text=msg)
        if not pf_ok:
            if getattr(args, "no_strict_preflight", False):
                log_fields(提示="存在告警项，因 --no-strict-preflight 继续执行")
            else:
                log_error("风控自检未通过，已阻断（加 --no-strict-preflight 可强制继续）")
                return 1

    if args.dry_run:
        run_record_code = "DRYRUN_RECORD_CODE"
        log_status("发起自由跑起跑", ok=True, status_text="dry-run 跳过")
    else:
        start_res = client.start_free_run(
            aim_distance=str(args.distance),
            lng=f"{args.lng:.6f}", lat=f"{args.lat:.6f}",
        )
        if start_res.get("code") != 0:
            log_status("发起自由跑起跑", ok=False)
            log_detail(start_res)
            return 1
        run_record_code = (start_res.get("data") or {}).get("runRecordCode")
        log_status("发起自由跑起跑", ok=True)
        log_fields(跑单编号=run_record_code)

    if args.stop_after == "start":
        log_status("停止于起跑", ok=True,
                   status_text="已创建跑单但未结算；如需清理请用 --abandon 或对应接口")
        log_fields(跑单编号=run_record_code)
        return 0

    # 轨迹区域必须落在学校真实围栏内。自由跑不校验围栏，但轨迹偏离操场
    # 会留下"不在跑道上"的痕迹，所以这里同样优先用 querySchoolFences 的真实
    # 多边形；只有拉不到时才退回 --lat/--lng 构造的矩形。
    fence = _resolve_fence(client, args)
    if args.lat and args.lng:
        log_fields(参考坐标=f"{args.lat:.6f},{args.lng:.6f}")
    log_fields(轨迹区域点数=len(fence))

    gen = te.generate_trajectory(
        target_distance_m=float(args.distance),
        avg_pace_sec_per_km=float(args.pace),
        fence=fence,
        mode=args.mode,
        seed=args.seed,
        point_codes=args.point_codes,
        lap_count=args.laps or None,
    )
    log_status("生成轨迹数据", ok=True)
    log_fields(距离=f"{gen.distance}m", 时长=f"{gen.duration}s", 总步数=gen.total_step,
               平均配速=f"{gen.avg_pace}s/km", 采样点=len(gen.pois),
               圈数=f"{gen.laps} × {gen.lap_length:.0f}m")

    # 步态对齐：引擎默认产出「跑步」步态（~0.9m 步幅，113 步/分）。
    # 若账号历史是「走路」步态（实测 0.56m、170 步/分），步频差 50%，
    # 本身就是特征。优先级：--stride > 历史中位 > 引擎默认。
    stride_used = None
    stride_src = ""
    if getattr(args, "stride", None):
        stride_used, stride_src = float(args.stride), "显式 --stride"
    elif getattr(args, "match_history", True) and not args.dry_run:
        try:
            import statistics as _st
            hh = client.page_free_run_record_list(page_num=1, page_size=10)
            hs = []
            for grp in (hh.get("data") or {}).get("list") or []:
                items = grp.get("list") if isinstance(grp, dict) and isinstance(grp.get("list"), list) else [grp]
                for rec in items:
                    if not isinstance(rec, dict):
                        continue
                    d, s = rec.get("distance"), rec.get("totalStep")
                    try:
                        if d and s and int(s) > 0:
                            hs.append(float(d) / float(s))
                    except Exception:
                        pass
            if len(hs) >= 2:
                stride_used = round(_st.median(hs), 2)
                stride_src = f"历史中位（{len(hs)} 单）"
        except Exception:
            stride_used = None
    if stride_used:
        before = gen.total_step
        te.apply_fixed_stride(gen, stride_used)
        log_fields(步态对齐=f"步幅 {stride_used}m（{stride_src}）",
                   总步数=f"{before} → {gen.total_step}",
                   步频=f"{gen.total_step / max(1, gen.duration) * 60:.0f} 步/分")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(gen.as_dict(), fh, ensure_ascii=False, indent=2)
        log_status(f"写出 {args.out}", ok=True)

    if args.dry_run:
        log_status("结算", ok=True, status_text="dry-run 跳过")
        return 0

    # 关键：服务端自己记录 startTime / endTime（墙钟），但采信我们上报的 duration。
    # 若起跑后立刻结算，会留下 "endTime-startTime=14s 而 duration=481s" 的矛盾，
    # 这是可被一致性校验抓到的特征。--realtime 会用真实等待把两者对齐。
    if args.realtime:
        wait_s = max(0, int(gen.duration))
        log_status("按真实时长等待", ok=True,
                   status_text=f"{wait_s}s（让 startTime/endTime 与 duration 对齐）")
        deadline = time.time() + wait_s
        last_report = 0.0
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                break
            time.sleep(min(1.0, remain))
            elapsed = wait_s - remain
            if elapsed - last_report >= 60:
                last_report = elapsed
                print(f"    ... 已等待 {int(elapsed)}s / {wait_s}s", flush=True)
        log_status("等待结束", ok=True, status_text="开始结算")
    else:
        log_fields(提示="未开启 --realtime，服务端记录的 startTime/endTime 间隔会短于 duration")

    fin = client.finish_free_run(
        run_record_code=run_record_code,
        distance=gen.distance,
        duration=gen.duration,
        total_step=gen.total_step,
        pois=gen.pois,
        step_list=gen.step_list,
        pace_list=gen.pace_list,
    )
    log_status("自由跑结算 finishFreeRun", ok=fin.get("code") == 0)
    if fin.get("code") != 0:
        log_detail(fin)
        return 1
    log_detail(fin.get("data"))
    # 跑后行为：真机不会跑完直接下线
    for label, res in post_run_behavior(client, run_record_code, "free", rng):
        log_status(label, ok=(res or {}).get("code") == 0)
    return 0


def _coords_to_fence(lat: float, lng: float, half_m: float = 260.0):
    """以给定坐标为中心构造一个矩形轨迹区域（自由跑不绑围栏，只需一个合理的落脚范围）。"""
    dlat = half_m / te.M_PER_DEG_LAT
    dlng = half_m / (te.M_PER_DEG_LAT * abs(math.cos(math.radians(lat))) or 1.0)
    return [(lat - dlat, lng - dlng), (lat - dlat, lng + dlng),
            (lat + dlat, lng + dlng), (lat + dlat, lng - dlng)]


def _resolve_fence(client, args):
    """围栏优先序：--fence 文件 > 学校真实围栏接口 > --lat/--lng 起点矩形 > 内置默认。

    注意：自由跑也必须走真实围栏。用矩形替代会生成偏离操场的轨迹
    （实测偏 176m，落在围栏外 112m）。
    """
    if args.fence:
        pts = load_fence_file(args.fence)
        if len(pts) >= 3:
            return pts
        log_error(f"围栏文件点位不足 3 个: {args.fence}")
    try:
        res = client.query_school_fences()
        pts = extract_fence_from_response(res, near_lat=args.lat or None,
                                         near_lng=args.lng or None)
        if len(pts) >= 3:
            log_status("拉取学校围栏", ok=True, status_text=f"{len(pts)} 个边界点")
            return pts
        log_status("拉取学校围栏", ok=False, status_text="响应未识别，改用命令行起点")
    except Exception as e:
        log_status("拉取学校围栏", ok=False, status_text=str(e))
    if args.lat and args.lng:
        log_fields(警告="未取得真实围栏，轨迹将以参考坐标为中心的矩形生成，可能不在操场内")
        return _coords_to_fence(args.lat, args.lng)
    return list(te.DEFAULT_FENCE)


def main():
    parser = argparse.ArgumentParser(description="长跑业务全链路调度工具（起跑/上传/结算）")
    parser.add_argument("-t", "--type", choices=["sun", "free"], default="sun",
                        help="跑步类型: sun (阳光跑, 默认), free (自由跑)")
    parser.add_argument("-d", "--distance", type=int, default=2000, help="目标里程 (米)")
    parser.add_argument("-p", "--pace", type=int, default=360, help="平均配速 (秒/公里)")
    parser.add_argument("--mode", default="hull", choices=["hull", "stadium", "ellipse", "rect"],
                        help="跑法: hull 沿围栏绕行(默认) / stadium 操场跑道 / ellipse 椭圆 / rect 矩形环路")
    parser.add_argument("--fence", help="围栏点位 JSON 文件")
    parser.add_argument("--plan-code", default="", help="长跑方案编号")
    parser.add_argument("--fence-code", default="", help="打卡电子围栏编号")
    parser.add_argument("--face-img", default="",
                        help="人脸打卡图片 URL（阳光跑必需；留空则不提交 faceCheckRecordList）")
    parser.add_argument("--no-strict-preflight", action="store_true",
                        help="风控自检告警时不阻断（默认阻断：时间窗/频率/间隔/IP 省）")
    parser.add_argument("--stride", type=float, default=None,
                        help="固定步幅（米）。指定后总步数按此重算，用于匹配账号历史步态")
    parser.add_argument("--match-history", action="store_true", default=True,
                        help="自动从历史自由跑推算中位步幅并对齐步态（默认开启）")
    parser.add_argument("--no-match-history", dest="match_history", action="store_false",
                        help="关闭历史步态对齐")
    parser.add_argument("--no-keepalive", dest="keepalive", action="store_false", default=True,
                        help="关闭跑前保活（默认开启：模拟 App 前台停留）")
    parser.add_argument("--lat", type=float, default=0.0, help="起跑基准纬度")
    parser.add_argument("--lng", type=float, default=0.0, help="起跑基准经度")
    parser.add_argument("--point-codes", default="", help="打卡点编号，逗号分隔")
    parser.add_argument("--seed", type=int, default=None, help="轨迹随机种子（复现用）")
    parser.add_argument("--speed", type=float, default=1.0, help="上传节奏倍率（仅调试用，1=按真机间隔）")
    parser.add_argument("--dry-run", action="store_true", help="只生成数据不联网")
    parser.add_argument("--stop-after", default="probe",
                        choices=["probe", "start", "full"],
                        help="分阶段放行: probe=只读探测(默认) / start=起跑后停 / full=起跑+结算")
    parser.add_argument("--laps", type=int, default=0,
                        help="固定圈数（0=自动取整，使总里程最接近 -d；操场单圈约 500m）")
    parser.add_argument("--realtime", action="store_true",
                        help="起跑后按真实时长等待再结算（使服务端 startTime/endTime 与 duration 一致，正式使用时建议开启）")
    parser.add_argument("--no-pre-behavior", action="store_true", help="跳过跑前浏览行为注入")
    parser.add_argument("--out", default="", help="把生成的跑单数据写出到 JSON 文件")
    parser.add_argument("--token", help="临时指定 Token 凭证")
    parser.add_argument("--satoken", help="临时指定 SaToken 凭证")
    parser.add_argument("--transport", default="direct", choices=["direct", "adb", "proxy"],
                        help="网络传输模式")
    parser.add_argument("--proxy", help="代理服务器地址")
    parser.add_argument("--adb-path", help="ADB 命令路径")
    parser.add_argument("--adb-device", help="ADB 目标设备编号")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    if args.point_codes:
        args.point_codes = [c for c in (x.strip() for x in args.point_codes.split(",")) if c]
    else:
        args.point_codes = None

    client = ShanDongClient(
        token=args.token,
        satoken=args.satoken,
        transport_mode=args.transport,
        proxy=args.proxy,
        adb_path=args.adb_path,
        adb_device=args.adb_device,
    )

    if args.dry_run:
        log_status("会话检查", ok=True, status_text="dry-run 跳过")
    else:
        if not client.transport.token:
            log_status("载入会话", ok=False)
            log_error("未检测到有效会话，请先执行 login_cli.py 进行验证")
            return 1
        log_status("载入会话", ok=True)

    log_fields(传输模式=args.transport, 设备机型=client.transport.model_name,
               系统版本=client.transport.system_version)

    # 阶段一：只读探测（不写入任何数据）
    if not args.dry_run and args.stop_after == "probe":
        log_status("阶段=probe", ok=True, status_text="只读探测，不创建跑单")
        return probe_only(client)

    # 跑前行为：先浏览再进跑步模块
    if not args.no_pre_behavior and not args.dry_run:
        log_status("跑前浏览行为", ok=True, status_text="注入")
        for label, res in pre_run_behavior(client, rng):
            ok = isinstance(res, dict) and res.get("code") == 0
            log_fields(**{label: "ok" if ok else f"code={res.get('code') if isinstance(res, dict) else '?'}"})
        # 保活：真机不会一打开 App 就起跑，中间会有几十秒到几分钟的正常停留
        if getattr(args, "keepalive", True):
            log_status("跑前保活", ok=True, status_text="模拟 App 前台停留")
            for label, res in keepalive(client, rng, rounds=rng.randint(1, 3)):
                ok = isinstance(res, dict) and res.get("code") == 0
                log_fields(**{label: "ok" if ok else "skip"})

    if args.type == "sun":
        return run_sun(client, args, rng)
    return run_free(client, args, rng)


if __name__ == "__main__":
    sys.exit(main())
