"""HAR 对比工具 —— 用真机抓包的请求体反向校验轨迹引擎的字段契约。

用途：把真机 App 抓包导出的 .har 丢进来，工具会：
  1. 抽出跑步链路的样本请求体（依据 URL 关键字自动识别接口）
  2. 从样本里推导真实字段清单 / 类型 / 量纲 / 分片间隔
  3. 跑一遍本地轨迹引擎，做**逐字段对比**，输出差异报告
  4. 直接给出需要改代码的结论（字段名错、单位错、分片间隔错、类型错）

用法：
    python har_diff.py capture.har                 # 只分析抓包
    python har_diff.py capture.har --compare       # 并跑当地引擎做对比
    python har_diff.py capture.har --dump pois     # 打印某接口样本原貌

不需要 token、不联网。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 接口识别：URL 关键字 -> 逻辑名
ROUTE_KEYS = [
    ("/run/startSunRun", "startSunRun"),
    ("/run/startFreeRun", "startFreeRun"),
    ("/run/uploadRunRecord", "uploadRunRecord"),
    ("/run/uploadStepsRecord", "uploadStepsRecord"),
    ("/run/uploadPaceRecord", "uploadPaceRecord"),
    ("/run/uploadStrideRecord", "uploadStrideRecord"),
    ("/run/uploadPassPoint", "uploadPassPoint"),
    ("/run/finishSunRun", "finishSunRun"),
    ("/run/finishFreeRun", "finishFreeRun"),
    ("/school/querySchoolFences", "querySchoolFences"),
    ("/school/filterUsableFence", "filterUsableFence"),
    ("/run/checkSunRunConfig", "checkSunRunConfig"),
    ("/run/getPreRunCheckIn", "getPreRunCheckIn"),
    ("/run/plan/selectList", "runPlanSelectList"),
]

# 本引擎产出的字段（与 trajectory_engine 的输出契约一一对应）
ENGINE_SCHEMA = {
    "startSunRun": {"runPlanCode", "targetDistance", "sportType", "lng", "lat", "useCreditSword"},
    "uploadRunRecord": {"runRecordCode", "pois"},
    "uploadStepsRecord": {"runRecordCode", "stepList", "stepInterval"},
    "uploadPaceRecord": {"runRecordCode", "paceList", "paceInterval"},
    "uploadStrideRecord": {"runRecordCode", "strideList", "strideInterval"},
    "uploadPassPoint": {"runRecordCode", "targetPoints"},
    "finishSunRun": {"runRecordCode", "distance", "stepInterval", "targetPoints",
                     "runImgRecord", "timestamp", "pauseCount", "pauseTimes",
                     "faceCheckRecordList", "duration"},
    "finishFreeRun": {"runRecordCode", "distance", "duration", "totalStep", "stepInterval",
                      "paceInterval", "status", "pois", "stepList", "paceList"},
}

POI_FIELDS = ["lat", "lng", "stability", "accuracy", "collectTime"]
STEP_FIELDS = ["index", "startTime", "endTime", "startStep", "endStep", "step", "time", "stability"]
PACE_FIELDS = ["index", "startTime", "endTime", "startDistance", "endDistance", "distance",
               "time", "startStepCount", "endStepCount", "stepCount", "stability"]
STRIDE_FIELDS = ["index", "time", "distance", "stride", "step"]
TP_FIELDS = ["code", "lat", "lng", "type", "passStatus", "clockTime"]


# ---------------------------------------------------------------------------
# HAR 解析
# ---------------------------------------------------------------------------

def load_har(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        return json.load(fh)


def route_of(url: str) -> Optional[str]:
    for key, name in ROUTE_KEYS:
        if key in url:
            return name
    return None


def extract_samples(har: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """把 HAR 归约为 {逻辑接口名: [请求体, ...]}。"""
    out: Dict[str, List[Dict[str, Any]]] = {}
    entries = (har.get("log") or {}).get("entries") or []
    for e in entries:
        req = e.get("request") or {}
        url = req.get("url") or ""
        name = route_of(url)
        if not name:
            continue
        text = ((req.get("postData") or {}).get("text") or "").strip()
        if not text:
            continue
        try:
            body = json.loads(text)
        except Exception:
            continue
        if isinstance(body, dict):
            out.setdefault(name, []).append(body)
    return out


def dump_entry_summary(samples: Dict[str, List[Dict[str, Any]]]) -> None:
    print("=" * 72)
    print("抓包样本概览")
    print("=" * 72)
    if not samples:
        print("  未识别到跑步链路接口。请确认 HAR 是手机 App 跑一次步的完整抓包。")
        return
    for name in sorted(samples):
        bodies = samples[name]
        b0 = bodies[0]
        arr_info = []
        for k, v in b0.items():
            if isinstance(v, list):
                arr_info.append(f"{k}[{len(v)}]")
        print(f"  {name:22s} 样本数={len(bodies):3d}  字段={sorted(b0.keys())}")
        if arr_info:
            print(f"  {'':22s} 数组字段: {', '.join(arr_info)}")
    print()


# ---------------------------------------------------------------------------
# 从样本推导真实契约
# ---------------------------------------------------------------------------

def type_name(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "dict"
    return type(v).__name__


def field_types(items: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """统计数组元素里每个字段出现的类型频次。"""
    acc: Dict[str, Dict[str, int]] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        for k, v in it.items():
            acc.setdefault(k, {})
            t = type_name(v)
            acc[k][t] = acc[k].get(t, 0) + 1
    return acc


def infer_interval(values: List[int], label: str, unit_hint: str) -> Optional[float]:
    """从时间戳序列推断分片间隔（秒）。"""
    vals = sorted(set(values))
    if len(vals) < 3:
        return None
    diffs = [b - a for a, b in zip(vals, vals[1:])]
    med = statistics.median(diffs)
    print(f"     {label}: 中位间隔={med:.0f} ({unit_hint}) "
          f"最小={min(diffs)} 最大={max(diffs)} 切片数={len(values)}")
    return med


def analyze_and_compare(samples: Dict[str, List[Dict[str, Any]]], do_compare: bool) -> int:
    issues: List[str] = []
    notes: List[str] = []

    print("=" * 72)
    print("逐接口契约核对")
    print("=" * 72)

    # --- 1. 字段集合对比 ---
    for name, expected in ENGINE_SCHEMA.items():
        bodies = samples.get(name) or []
        if not bodies:
            notes.append(f"{name}: 抓包里没有，跳过核对")
            continue
        got = set(bodies[0].keys())
        missing = expected - got
        extra = got - expected
        mark = "OK" if not missing else "差异"
        print(f"\n[{mark}] {name}")
        print(f"     真机字段 : {sorted(got)}")
        if missing:
            print(f"     !! 我们少了 : {sorted(missing)}")
            issues.append(f"{name} 缺少字段 {sorted(missing)}")
        if extra:
            print(f"     ?? 我们多了 : {sorted(extra)}")
            issues.append(f"{name} 多出字段 {sorted(extra)}")

    # --- 2. 数组元素字段与类型 ---
    check_matrix = [
        ("uploadRunRecord", "pois", POI_FIELDS),
        ("uploadStepsRecord", "stepList", STEP_FIELDS),
        ("uploadPaceRecord", "paceList", PACE_FIELDS),
        ("uploadStrideRecord", "strideList", STRIDE_FIELDS),
        ("uploadPassPoint", "targetPoints", TP_FIELDS),
        ("finishSunRun", "targetPoints", TP_FIELDS),
    ]
    for route, arr_key, our_fields in check_matrix:
        bodies = samples.get(route) or []
        if not bodies:
            continue
        items = []
        for b in bodies:
            v = b.get(arr_key)
            if isinstance(v, list):
                items.extend(v)
        if not items:
            notes.append(f"{route}.{arr_key}: 抓包里为空数组，字段无法核对")
            continue
        ftypes = field_types(items)
        got = set(ftypes.keys())
        print(f"\n--- {route}.{arr_key}  (元素数={len(items)}) ---")
        for f in sorted(got):
            dist = ", ".join(f"{t}×{c}" for t, c in ftypes[f].items())
            flag = "" if f in our_fields else "   <-- 我们没有的字段"
            print(f"     {f:18s} {dist}{flag}")
        missing = set(our_fields) - got
        if missing:
            print(f"     !! 我们多了这些字段: {sorted(missing)}")
            issues.append(f"{route}.{arr_key} 我们多了 {sorted(missing)}")

    # --- 3. 量纲核对：毫秒 vs 秒 ---
    print("\n" + "=" * 72)
    print("量纲核对（time 字段单位 / 时间基准）")
    print("=" * 72)
    for route, arr_key, tkey in (("uploadStepsRecord", "stepList", "time"),
                                 ("uploadPaceRecord", "paceList", "time"),
                                 ("uploadStrideRecord", "strideList", "time")):
        bodies = samples.get(route) or []
        vals = []
        for b in bodies:
            for it in (b.get(arr_key) or []):
                if isinstance(it, dict) and tkey in it:
                    vals.append(it[tkey])
        if vals:
            med = statistics.median(vals)
            guess = ("毫秒(相对)" if med < 60000 else
                     "毫秒(绝对epoch)" if med > 1e12 else
                     "秒/待定")
            print(f"  {route}.{arr_key}.{tkey}: 中位={med:.0f} -> 推测 {guess}")

    for route, arr_key, tkey in (("uploadStepsRecord", "stepList", "startTime"),
                                 ("uploadPaceRecord", "paceList", "startTime"),
                                 ("uploadPassPoint", "targetPoints", "clockTime"),
                                 ("uploadRunRecord", "pois", "collectTime")):
        bodies = samples.get(route) or []
        vals = []
        for b in bodies:
            for it in (b.get(arr_key) or []):
                if isinstance(it, dict) and tkey in it:
                    vals.append(it[tkey])
        if vals and min(vals) > 1e12:
            has_sub = any(v % 1000 for v in vals)
            print(f"  {route}.{arr_key}.{tkey}: 绝对 epoch 毫秒，"
                  f"非整秒占比={sum(1 for v in vals if v % 1000)/len(vals):.1%}")
            if not has_sub:
                notes.append(f"{route}.{arr_key}.{tkey} 真机是整秒，我们的亚秒抖动可能不一致")
        elif vals:
            print(f"  {route}.{arr_key}.{tkey}: 最小={min(vals)} -> 相对时间戳")

    # --- 4. 分片间隔 ---
    print("\n" + "=" * 72)
    print("分片间隔核对")
    print("=" * 72)
    for route, arr_key, tkey in (("uploadStepsRecord", "stepList", "startTime"),
                                 ("uploadPaceRecord", "paceList", "startTime"),
                                 ("uploadStrideRecord", "strideList", "time")):
        bodies = samples.get(route) or []
        vals = []
        for b in bodies:
            for it in (b.get(arr_key) or []):
                if isinstance(it, dict) and tkey in it:
                    vals.append(it[tkey])
        if vals:
            infer_interval(vals, f"{arr_key}.{tkey}", "ms")

    # --- 5. pois 采样间隔与 accuracy ---
    bodies = samples.get("uploadRunRecord") or []
    pois = []
    for b in bodies:
        pois.extend(b.get("pois") or [])
    if pois:
        ct = sorted(p["collectTime"] for p in pois if "collectTime" in p)
        if len(ct) > 2:
            diffs = [b - a for a, b in zip(ct, ct[1:])]
            print(f"\n  pois.collectTime 间隔: 中位={statistics.median(diffs):.0f}ms "
                  f"p05={sorted(diffs)[len(diffs)//20]} p95={sorted(diffs)[-max(1, len(diffs)//20)]} "
                  f"最小={min(diffs)} 最大={max(diffs)}")
            print(f"     我们生成: 中位≈2000ms 范围 1000~3000ms + 6% 漏采")
        accs = [p.get("accuracy") for p in pois if isinstance(p.get("accuracy"), (int, float))]
        if accs:
            print(f"  pois.accuracy: 均值={statistics.fmean(accs):.2f} "
                  f"σ={statistics.pstdev(accs):.2f} 最小={min(accs)} 最大={max(accs)} "
                  f"小数位样本={[str(a) for a in accs[:5]]}")
        st = [p.get("stability") for p in pois if "stability" in p]
        if st:
            print(f"  pois.stability 分布: " +
                  ", ".join(f"{v}×{st.count(v)}" for v in sorted(set(st))))

    # --- 6. 我们引擎的对照跑 ---
    if do_compare:
        print("\n" + "=" * 72)
        print("本地引擎对照（同距离/配速生成一份，字段级比对）")
        print("=" * 72)
        try:
            import trajectory_engine as te
            dist = 2000.0
            fin = samples.get("finishSunRun") or samples.get("finishFreeRun") or [{}]
            try:
                dist = float(fin[0].get("distance") or 2000)
            except Exception:
                pass
            run = te.generate_trajectory(target_distance_m=dist, avg_pace_sec_per_km=360.0,
                                        seed=11, mode="hull")
            ours = {
                "pois": POI_FIELDS,
                "stepList": STEP_FIELDS,
                "paceList": PACE_FIELDS,
                "strideList": STRIDE_FIELDS,
                "targetPoints": TP_FIELDS,
            }
            mine = {
                "pois": run.pois,
                "stepList": run.step_list,
                "paceList": run.pace_list,
                "strideList": run.stride_list,
                "targetPoints": run.target_points,
            }
            for key, fields in ours.items():
                got_f = set(field_types(mine[key]).keys())
                print(f"  {key:14s} 我们产出字段={sorted(got_f)}  "
                      f"元素数={len(mine[key])}")
                miss = set(fields) - got_f
                if miss:
                    issues.append(f"本地引擎 {key} 缺字段 {sorted(miss)}")
            print(f"  本地样例: distance={run.distance} duration={run.duration} "
                  f"totalStep={run.total_step} avgPace={run.avg_pace} "
                  f"pause={run.pause_count}段/{run.pause_times}s capped={run.capped}")
        except Exception as e:
            notes.append(f"本地引擎对照失败: {e}")

    # --- 结论 ---
    print("\n" + "=" * 72)
    print("结论")
    print("=" * 72)
    if issues:
        print(f"发现 {len(issues)} 处需要改代码的差异：")
        for i, s in enumerate(issues, 1):
            print(f"  {i}. {s}")
    else:
        print("  未发现字段级差异 —— 我们的请求体契约与真机一致。")
    if notes:
        print("\n备注（无法自动判定 / 需人工确认）：")
        for n in notes:
            print(f"  - {n}")
    return 1 if issues else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="用真机 HAR 校验轨迹引擎字段契约")
    ap.add_argument("har", help="抓包导出的 .har 文件")
    ap.add_argument("--compare", action="store_true", help="同时跑本地引擎做字段比对")
    ap.add_argument("--dump", help="打印指定接口的第一个请求体原貌")
    args = ap.parse_args()

    if not os.path.exists(args.har):
        print(f"[错误] 文件不存在: {args.har}")
        return 2
    har = load_har(args.har)
    samples = extract_samples(har)
    dump_entry_summary(samples)

    if args.dump:
        bodies = samples.get(args.dump)
        if not bodies:
            print(f"[错误] 抓包里没有 {args.dump}")
            return 2
        print(json.dumps(bodies[0], ensure_ascii=False, indent=2)[:6000])
        print()
    return analyze_and_compare(samples, args.compare)


if __name__ == "__main__":
    sys.exit(main())
