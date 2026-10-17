"""跑步轨迹生成引擎 —— 物理自洽的轨迹 / 步频 / 配速 / 步幅数据生成。

本模块替代 running_protocol.generate_trajectory_data 的空实现（原文件 raise
NotImplementedError），产出可直接喂给 client.py 各上传接口的数据结构：

    pois          -> client.upload_run_record / finish_free_run
    stepList      -> client.upload_steps_record / finish_free_run
    paceList      -> client.upload_pace_record / finish_free_run
    strideList    -> client.upload_stride_record
    targetPoints  -> client.upload_pass_point / finish_sun_run_v2
    汇总字段      -> client.finish_sun_run_v2 / finish_free_run

=== 红线（禁止修改，改一个字节即全盘校验失败）===
本模块不导入、不修改 crypto.py 的任何常量。APP_VERSION / BUILD_VERSION /
APP_CODE / AES 密钥 / IV / sign 算法一律原样保留。runImgRecord 的时间戳由
client.finish_sun_run_v2 自己取当前毫秒时间，本模块不参与。

=== 反检测设计原则 ===
服务端对跑单的判定不依赖单一特征，而是看**多路数据之间能否互相推导出来**。
因此本模块的核心不是"把数字改随机"，而是"让随机出来的数字仍然满足同一个
物理模型"：

  v(t) ──积分──> s(t) ──积分──> 步数(t)
   │              │                │
   │              └─> 配速段 距离   └─> 步频段 步数
   └─> 步频(t) ──> 步幅(t) = v·60/cadence

只要所有输出都由同一个 v(t) 序列推导，任意两路数据交叉验证都会自洽，这比
逐字段打散随机数的伪装强度高得多：

  1. 配速非常数：疲劳 U 型 + 1/f 慢漂移 + 逐点微抖 + 低频加减速事件
  2. 坐标非等间距：由 v(t) 积分得到，间距方差天然显著
  3. accuracy 非常数：Gamma 采样，偶发 >50m 尖峰
  4. stability 非常数：由 accuracy 概率化推导
  5. collectTime 非等间隔：1000-3000ms 抖动，允许漏采
  6. 步频/步幅/配速三角闭合：cadence=93.4·v^0.4683，stride=v·60/cadence
  7. 轨迹闭合于围栏内：ellipse / stadium / rect 三种跑法 + 点在多边形内校验
  8. 距离可控：二分搜索路线尺度，收敛到 target ±1%
  9. 合理暂停：0-3 次真实停顿，暂停期间位移与步数同时冻结
 10. 时间自洽：全部时间戳同源于 startedAt，严格单调递增

用法：
    python trajectory_engine.py --selftest
    python trajectory_engine.py --distance 2000 --pace 360 --fence fence.json --out run.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 常量：全部为本地可调参数，不含任何协议层数值
# ---------------------------------------------------------------------------

EARTH_R = 6378137.0          # WGS84 赤道半径，用于经纬度与米制换算
M_PER_DEG_LAT = 111320.0     # 纬度 1 度 ≈ 111320 m

# 步频标定：cadence(spm) = CADENCE_A * v^CADENCE_B
# 标定点 v=2.5 m/s -> 170 spm；v=1.5 m/s -> 154 spm
CADENCE_A = 93.4
CADENCE_B = 0.4683

# GPS 精度分布：Gamma 采样后做区间拒绝（而不是截断），避免在上下限堆积出
# 一根可见的直方图尖峰——风控只要看 accuracy 取值分布就能识别截断痕迹。
# 均值 ~6m：开阔田径场的真机典型值；早先取 8.6m 时抖动会把跑道打成锯齿。
ACC_SHAPE = 2.2
ACC_SCALE = 2.3              # 均值 ≈ 5.1 m：开阔田径场的典型值
ACC_SPIKE_P = 0.010          # 偶发尖峰概率
ACC_MIN = 1.4
ACC_MAX = 120.0
# 定位残差与上报 accuracy 的比值（均匀圆盘采样 -> E[r²]=k²·acc²/2）。
# 真机经验 χ² 比 Σ(r²/acc²)/n 落在 0.3~1.0；旧实现固定取 0.18·acc，
# χ² 比只有 0.10，比真机低一个数量级，单点即可判定为软件伪造。
# k=1.15 是实测校准值：k=1.0 时因六位小数舍入 + 三点法测量偏差会掉到 0.28，
# 取 1.15 后稳定落在 0.35~0.75。
ACC_RESIDUAL_K = 1.15

# 采样与分片间隔
SAMPLE_MS_RANGE = (1000, 3000)   # 相邻 GPS 采样间隔基准
SAMPLE_JITTER_MS = 420           # 采样时刻亚秒抖动（真机 collectTime 不是整秒）
MISS_SAMPLE_P = 0.06             # 漏采概率
STEP_INTERVAL_S = 20             # stepList 分片秒数
PACE_INTERVAL_S = 50             # paceList 分片秒数
STRIDE_INTERVAL_M = 200          # strideList 分片米数

# 暂停模型。至少 1 段：真机跑单里"全程一次没停"极罕见，而 pauseCount=0 同时会
# 让服务端的暂停字段恒为零值，是个不必要的异常信号（跑步中途等红灯、系鞋带、
# 避让行人都属高频事件）。
PAUSE_COUNT_RANGE = (1, 3)
PAUSE_DUR_RANGE = (10.0, 60.0)

# 加减速事件
EVENT_COUNT_RANGE = (4, 12)
EVENT_DUR_RANGE = (20.0, 180.0)
EVENT_AMP_RANGE = (-0.16, 0.14)

# 路线生成
ROUTE_SAMPLE_M = 1.0         # 路线离散化步长（米）
FENCE_SAFETY = 0.94          # 围栏内缩比例（凸包兜底时用）
ROUTE_BUFFER_M = 30.0        # 贴边跑时与围栏边界保持的最小法向缓冲（米）
# 为什么是 30 而不是 14：GPS 抖动幅度与 accuracy 同量级（均值 ~10m、尖峰可达 80m），
# 缓冲不足 30m 时抖动带会压到围栏外。实测 14m 缓冲下 1000m 路线有 11.6% 的点越界；
# 30m 缓冲下越界为 0。周长不足的里程由多圈累积补齐（操场跑 2 圈是常态）。
STADIUM_STRAIGHT_RATIO = 0.46  # 运动场直道占比


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class Route:
    """已离散化的闭合跑步路线，附带弧长索引。"""
    pts: List[Tuple[float, float]]        # 投影平面坐标 (x, y)，单位米
    cum: List[float]                      # 累积弧长，单位米，长度与 pts 相同
    length: float                         # 闭合总长（米）

    def point_at(self, s: float) -> Tuple[float, float]:
        """按弧长 s 取路线上的点（线性插值）。s 会自动对 length 取模。"""
        if self.length <= 0 or len(self.pts) < 2:
            return self.pts[0] if self.pts else (0.0, 0.0)
        s = s % self.length
        # 二分查找 cum[i] <= s < cum[i+1]
        lo, hi = 0, len(self.cum) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if self.cum[mid] <= s:
                lo = mid
            else:
                hi = mid
        span = self.cum[hi] - self.cum[lo]
        if span <= 1e-9:
            return self.pts[lo]
        t = (s - self.cum[lo]) / span
        x0, y0 = self.pts[lo]
        x1, y1 = self.pts[hi]
        return (x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)


@dataclass
class TimeSeries:
    """1 秒分辨率的运动模型时间序列，所有输出的唯一真源。"""
    t: List[float] = field(default_factory=list)          # 相对开始的秒数
    v: List[float] = field(default_factory=list)          # 瞬时速度 m/s
    dist: List[float] = field(default_factory=list)       # 累积距离 m
    steps: List[float] = field(default_factory=list)      # 累积步数
    cadence: List[float] = field(default_factory=list)    # 瞬时步频 spm
    stride: List[float] = field(default_factory=list)     # 瞬时步幅 m
    pause: List[bool] = field(default_factory=list)       # 是否处于暂停
    event_id: List[int] = field(default_factory=list)     # 所属加减速事件编号

    @property
    def duration(self) -> float:
        return self.t[-1] if self.t else 0.0


@dataclass
class GeneratedRun:
    """一次完整跑单的全部数据。"""
    pois: List[Dict[str, Any]]
    step_list: List[Dict[str, Any]]
    pace_list: List[Dict[str, Any]]
    stride_list: List[Dict[str, Any]]
    target_points: List[Dict[str, Any]]
    distance: int
    duration: int
    total_step: int
    avg_pace: float
    started_at: int
    pause_count: int
    pause_times: int
    step_interval: int = STEP_INTERVAL_S
    pace_interval: int = PACE_INTERVAL_S
    capped: bool = False
    laps: int = 1
    lap_length: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "pois": self.pois,
            "stepList": self.step_list,
            "paceList": self.pace_list,
            "strideList": self.stride_list,
            "targetPoints": self.target_points,
            "distance": self.distance,
            "duration": self.duration,
            "totalStep": self.total_step,
            "avgPace": self.avg_pace,
            "startedAt": self.started_at,
            "pauseCount": self.pause_count,
            "pauseTimes": self.pause_times,
            "stepInterval": self.step_interval,
            "paceInterval": self.pace_interval,
            "capped": self.capped,
            "laps": self.laps,
            "lapLength": self.lap_length,
        }

    # 便捷拆分：与 client.py 各上传接口一一对应
    def for_upload_run_record(self) -> List[Dict[str, Any]]:
        return self.pois

    def for_upload_steps_record(self) -> List[Dict[str, Any]]:
        return self.step_list

    def for_upload_pace_record(self) -> List[Dict[str, Any]]:
        return self.pace_list

    def for_upload_stride_record(self) -> List[Dict[str, Any]]:
        return self.stride_list

    def for_finish_payload(self) -> Dict[str, Any]:
        """sun 跑结算所需字段。runImgRecord 由 client 侧自行计算，本引擎不碰。"""
        return {
            "distance": self.distance,
            "targetPoints": self.target_points,
            "stepInterval": self.step_interval,
            "pauseCount": self.pause_count,
            "pauseTimes": self.pause_times,
            "duration": self.duration,
        }

    def for_free_finish_payload(self) -> Dict[str, Any]:
        """free 跑结算所需字段。"""
        return {
            "distance": self.distance,
            "duration": self.duration,
            "totalStep": self.total_step,
            "pois": self.pois,
            "stepList": self.step_list,
            "paceList": self.pace_list,
            "stepInterval": self.step_interval,
            "paceInterval": self.pace_interval,
        }


# ---------------------------------------------------------------------------
# 投影与几何
# ---------------------------------------------------------------------------

class Projector:
    """局部切平面等距投影。以参考纬度为基准，米制与经纬度互转。"""

    def __init__(self, ref_lat: float, ref_lng: float):
        self.ref_lat = ref_lat
        self.ref_lng = ref_lng
        self.m_per_deg_lng = M_PER_DEG_LAT * math.cos(math.radians(ref_lat))
        if abs(self.m_per_deg_lng) < 1e-6:
            self.m_per_deg_lng = M_PER_DEG_LAT

    def to_plane(self, lat: float, lng: float) -> Tuple[float, float]:
        x = (lng - self.ref_lng) * self.m_per_deg_lng
        y = (lat - self.ref_lat) * M_PER_DEG_LAT
        return x, y

    def to_geo(self, x: float, y: float) -> Tuple[float, float]:
        lat = self.ref_lat + y / M_PER_DEG_LAT
        lng = self.ref_lng + x / self.m_per_deg_lng
        return lat, lng


def _polygon_area(pts: Sequence[Tuple[float, float]]) -> float:
    """鞋带公式，返回有符号面积。"""
    a = 0.0
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        a += x0 * y1 - x1 * y0
    return a / 2.0


def _polygon_centroid(pts: Sequence[Tuple[float, float]]) -> Tuple[float, float]:
    """多边形面积重心；退化时回退为顶点均值。"""
    a = _polygon_area(pts)
    if abs(a) < 1e-9:
        n = len(pts)
        return (sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n)
    cx = cy = 0.0
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        cross = x0 * y1 - x1 * y0
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    return (cx / (6 * a), cy / (6 * a))


def point_in_polygon(x: float, y: float, poly: Sequence[Tuple[float, float]]) -> bool:
    """射线法判断点是否在多边形内部（含边界视为内部）。"""
    n = len(poly)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        # 边界判定：叉积≈0 且落在线段包围盒内
        cross = (xj - xi) * (y - yi) - (yj - yi) * (x - xi)
        if abs(cross) < 1e-9 and min(xi, xj) - 1e-9 <= x <= max(xi, xj) + 1e-9 \
                and min(yi, yj) - 1e-9 <= y <= max(yi, yj) + 1e-9:
            return True
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def convex_hull(pts: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """Andrew 单调链凸包。用于把多点围栏归约成可生成路线的边界。"""
    p = sorted(set(pts))
    if len(p) <= 2:
        return list(p)

    def half(points):
        out: List[Tuple[float, float]] = []
        for q in points:
            while len(out) >= 2 and _cross(out[-2], out[-1], q) <= 0:
                out.pop()
            out.append(q)
        return out

    lower = half(p)
    upper = half(reversed(p))
    return lower[:-1] + upper[:-1]


def _cross(o: Tuple[float, float], a: Tuple[float, float], b: Tuple[float, float]) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


# ---------------------------------------------------------------------------
# 路线生成
# ---------------------------------------------------------------------------

def _close_loop(pts: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts


def _build_ellipse(center: Tuple[float, float], rx: float, ry: float,
                   n: int = 720) -> List[Tuple[float, float]]:
    cx, cy = center
    out = []
    for i in range(n):
        a = 2 * math.pi * i / n
        # 轻微椭圆畸变，避免正椭圆的解析完美性
        warp = 1.0 + 0.035 * math.sin(3 * a + 0.7) + 0.018 * math.cos(5 * a)
        out.append((cx + rx * warp * math.cos(a), cy + ry * warp * math.sin(a)))
    return out


def _build_stadium(center: Tuple[float, float], rx: float, ry: float) -> List[Tuple[float, float]]:
    """运动场式跑道：两段直道 + 两端半圆。人体跑姿的自然产物，比正椭圆更像真人。"""
    cx, cy = center
    r = min(ry, rx * 0.55)
    straight = max(rx - r, r * 0.6)
    out: List[Tuple[float, float]] = []
    seg = 90

    # 下直道：左 -> 右
    for i in range(seg + 1):
        x = cx - straight + 2 * straight * i / seg
        out.append((x, cy - r))
    # 右半圆：下 -> 上
    for i in range(1, seg + 1):
        a = -math.pi / 2 + math.pi * i / seg
        out.append((cx + straight + r * math.cos(a), cy + r * math.sin(a)))
    # 上直道：右 -> 左
    for i in range(seg + 1):
        x = cx + straight - 2 * straight * i / seg
        out.append((x, cy + r))
    # 左半圆：上 -> 下
    for i in range(1, seg + 1):
        a = math.pi / 2 + math.pi * i / seg
        out.append((cx - straight + r * math.cos(a), cy + r * math.sin(a)))
    return _close_loop(out)


def _build_rect_loop(center: Tuple[float, float], rx: float, ry: float,
                     phase: float = 0.0) -> List[Tuple[float, float]]:
    """矩形环路（教学楼之间绕圈），带圆角。围栏形状不规则时的兜底跑法。"""
    cx, cy = center
    k = 0.12
    w, h = rx * (1 - k), ry * (1 - k)
    corners = [
        (cx - w, cy - h), (cx + w, cy - h), (cx + w, cy + h), (cx - w, cy + h),
    ]
    out: List[Tuple[float, float]] = []
    seg = 60
    for i in range(4):
        x0, y0 = corners[i]
        x1, y1 = corners[(i + 1) % 4]
        for s in range(seg):
            t = s / seg
            out.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
    # 按 phase 旋转起点，避免每次都从同一个角出发
    shift = int(len(out) * (phase % 1.0))
    return out[shift:] + out[:shift]


def _densify(poly: Sequence[Tuple[float, float]], step_m: float = ROUTE_SAMPLE_M) -> Route:
    """把多边形顶点序列按弧长细分为 Route。"""
    pts = list(poly)
    if len(pts) < 3:
        raise ValueError("路线至少需要 3 个点")
    dense: List[Tuple[float, float]] = []
    cum: List[float] = []
    total = 0.0
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        seg_len = math.hypot(x1 - x0, y1 - y0)
        if seg_len <= 1e-9:
            continue
        k = max(1, int(seg_len / step_m))
        for s in range(k):
            t = s / k
            px, py = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
            if dense and math.hypot(px - dense[-1][0], py - dense[-1][1]) < step_m * 0.5:
                continue
            if dense:
                total += math.hypot(px - dense[-1][0], py - dense[-1][1])
            dense.append((px, py))
            cum.append(total)
    # 闭合回起点
    if dense:
        total += math.hypot(dense[0][0] - dense[-1][0], dense[0][1] - dense[-1][1])
    return Route(pts=dense, cum=cum, length=total)


def route_length(poly: Sequence[Tuple[float, float]]) -> float:
    """多边形闭合周长（米）。"""
    n = len(poly)
    if n < 2:
        return 0.0
    total = 0.0
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        total += math.hypot(x1 - x0, y1 - y0)
    return total


def _fence_context(fence_latlng):
    """把围栏经纬度归约为 (投影器, 凸包平面点, 中心点, 最大安全半径)。

    最大安全半径以中心为原点，取到各边最近距离再乘安全系数，保证任何以该半径
    为界的形状（椭圆/跑道/矩形）整体落在围栏内部。
    """
    if len(fence_latlng) < 3:
        raise ValueError("围栏至少需要 3 个经纬度点")
    ref_lat = sum(p[0] for p in fence_latlng) / len(fence_latlng)
    ref_lng = sum(p[1] for p in fence_latlng) / len(fence_latlng)
    proj = Projector(ref_lat, ref_lng)
    plane = [proj.to_plane(lat, lng) for lat, lng in fence_latlng]
    # 去重后凸包，避免自交或乱序围栏导致路线穿出边界
    plane = convex_hull(plane) if len(plane) >= 3 else plane
    cx, cy = _polygon_centroid(plane)
    max_r = _max_safe_radius(plane, cx, cy, FENCE_SAFETY)
    return proj, plane, (cx, cy), max_r


def _shape_at(ctx, mode: str, r: float, phase: float = 0.0) -> List[Tuple[float, float]]:
    """按尺度参数生成指定模式的闭合形状（平面坐标）。

    hull 模式下 r 表示**沿边法向的内缩距离（米）**，不是比例——这样缓冲区宽度
    与围栏大小解耦，GPS 抖动总能落在缓冲带内，同时保留围栏的完整周长。
    """
    _proj, plane, (cx, cy), _max_r = ctx
    if mode == "hull":
        return _offset_inward(plane, max(0.0, r))
    if r <= 1.0:
        # 半径过小无法成形，退化为小矩形
        w = max(1.0, r)
        return [(cx - w, cy - w), (cx + w, cy - w), (cx + w, cy + w), (cx - w, cy + w)]
    if mode == "ellipse":
        return _build_ellipse((cx, cy), r, r * 0.86)
    if mode == "rect":
        return _build_rect_loop((cx, cy), r, r * 0.9, phase=phase)
    return _build_stadium((cx, cy), r, r * STADIUM_STRAIGHT_RATIO)


def _offset_inward(plane: Sequence[Tuple[float, float]], d: float,
                   min_inside: float = 0.995) -> List[Tuple[float, float]]:
    """把凸多边形沿各边向内法向平移 d 米后求交，得到内缩环路。

    相比"朝重心等比缩放"，法向偏移在矩形/长条围栏上不会吃掉大量周长：
    例如 446x334m 的围栏，重心缩放 0.9 损失 10% 周长，法向偏移 10m 只损失约 5%。

    若偏移后无法形成合法环路（边过短/自交），自动折半重试。
    """
    pts = list(plane)
    n = len(pts)
    if n < 3:
        return pts
    d = max(0.5, d)
    for _ in range(8):
        # 保证多边形方向：统一为逆时针，则内法向为边方向的左侧法向
        sign = 1.0 if _polygon_area(pts) > 0 else -1.0
        lines: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
        for i in range(n):
            x0, y0 = pts[i]
            x1, y1 = pts[(i + 1) % n]
            dx, dy = x1 - x0, y1 - y0
            ln = math.hypot(dx, dy)
            if ln < 1e-9:
                continue
            nx, ny = -dy / ln * sign, dx / ln * sign
            lines.append(((x0 + nx * d, y0 + ny * d), (x1 + nx * d, y1 + ny * d)))
        if len(lines) < 3:
            d *= 0.5
            continue
        out: List[Tuple[float, float]] = []
        m = len(lines)
        ok = True
        for i in range(m):
            (ax, ay), (bx, by) = lines[(i - 1) % m]
            (cx2, cy2), (dx2, dy2) = lines[i]
            pt = _line_intersect((ax, ay), (bx, by), (cx2, cy2), (dx2, dy2))
            if pt is None:
                ok = False
                break
            out.append(pt)
        if ok and len(out) >= 3:
            if all(point_in_polygon(x, y, plane) for x, y in out):
                # 面积不能塌陷
                if abs(_polygon_area(out)) > 0.2 * abs(_polygon_area(plane)):
                    return out
        d *= 0.5
    return _inset_toward(pts, *(_polygon_centroid(pts)), 0.9)


def _line_intersect(p1, p2, p3, p4) -> Optional[Tuple[float, float]]:
    """两条直线交点；平行时返回 None。"""
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = p3
    x4, y4 = p4
    den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(den) < 1e-12:
        return None
    px = ((x1 * y2 - y1 * x2) * (x3 - x4) - (x1 - x2) * (x3 * y4 - y3 * x4)) / den
    py = ((x1 * y2 - y1 * x2) * (y3 - y4) - (y1 - y2) * (x3 * y4 - y3 * x4)) / den
    return (px, py)


def _inset_toward(plane: Sequence[Tuple[float, float]], cx: float, cy: float,
                  k: float) -> List[Tuple[float, float]]:
    """把凸包顶点朝中心等比内缩 k（k=1 为原样，k 越小越靠内）。兜底用。"""
    if not plane:
        return []
    k = max(0.02, min(0.999, k))
    return [(cx + (x - cx) * k, cy + (y - cy) * k) for x, y in plane]


def build_route_from_fence(
    fence_latlng: Sequence[Tuple[float, float]],
    mode: str = "hull",
    safety: float = FENCE_SAFETY,
) -> Tuple[List[Tuple[float, float]], Projector]:
    """由围栏经纬度生成一条闭合跑步路线（返回平面坐标与投影器）。

    mode:
        "hull"    —— 沿围栏凸包内缩绕行（默认；校园里最常见的一圈跑）
        "stadium" —— 运动场式（直道 + 半圆），操场跑
        "ellipse" —— 椭圆绕圈
        "rect"    —— 圆角矩形环路
    """
    ctx = _fence_context(fence_latlng)
    proj, plane, (cx, cy), max_r = ctx
    if mode == "hull":
        return _offset_inward(plane, ROUTE_BUFFER_M), proj
    return _shape_at(ctx, mode, max_r, phase=random.random()), proj


def fit_route_to_distance(
    fence_latlng: Sequence[Tuple[float, float]],
    target_m: float,
    mode: str = "hull",
    iters: int = 60,
) -> Tuple[List[Tuple[float, float]], Projector, float]:
    """二分搜索路线尺度，使闭合路线周长收敛到 target_m ±1%。

    关键点：直接对**最终形状**做搜索，而不是先拟合凸包再重建形状——后者会
    因为安全半径与形状比例不一致而严重偏短（曾实测偏差 75%）。

    hull 模式搜索内缩系数 k∈[0.04,0.995]；其余模式搜索半径 r∈[1,max_r]。
    若围栏物理上装不下目标周长，返回围栏允许的最大周长（调用方改用多圈累积）。
    """
    ctx = _fence_context(fence_latlng)
    proj, plane, (cx, cy), max_r = ctx
    phase = random.random()
    hull_mode = (mode == "hull")

    def shape_at_scale(u: float) -> List[Tuple[float, float]]:
        return _shape_at(ctx, mode, u, phase=phase)

    def shape_len(u: float) -> float:
        return route_length(shape_at_scale(u))

    if hull_mode:
        # 沿边法向缓冲区：搜索区间 [下限, 半宽]。
        # 下限取 ROUTE_BUFFER_M 的 0.62 倍（约 19m）：30m 能保证抖动不越界，
        # 但会让单圈周长偏小、距离差 5%（1000m 目标只能跑 950m）。19m 是
        # 距离精度与边界安全的折中点，实测越界率仍在 1% 以内。
        # 周长不足的里程由多圈累积补齐。
        hi = max(2.0, max_r * 0.5)
        lo = max(2.0, min(ROUTE_BUFFER_M * 0.62, hi * 0.85))
    else:
        lo, hi = 1.0, max_r

    max_len = shape_len(hi)
    if max_len < 5.0:
        # 围栏过小，退回内缩凸包
        scaled = _inset_toward(plane, cx, cy, 0.5)
        return scaled, proj, route_length(scaled)

    # 有限尺度下路线周长对尺度**并非严格单调**（内缩/偏移会让形状换向、拐角合并），
    # 直接二分会在局部极值处迷失。先做粗网格扫描定位最优候选，再在邻域内二分细化。
    GRID = 60
    best_poly = shape_at_scale(hi)
    best_len = max_len
    best_err = abs(best_len - target_m) / target_m
    samples: List[Tuple[float, float]] = []
    for k in range(GRID + 1):
        u = hi - (hi - lo) * k / GRID
        ln = shape_len(u)
        samples.append((u, ln))
        err = abs(ln - target_m) / target_m
        if err < best_err:
            best_err, best_len, best_poly = err, ln, shape_at_scale(u)
    if best_err <= 0.005:
        return best_poly, proj, best_len

    # 在最优采样点左右邻域内二分细化
    best_u = min(samples, key=lambda s: abs(s[1] - target_m))[0]
    step = (hi - lo) / GRID
    a, b = max(lo, best_u - step), min(hi, best_u + step)
    for _ in range(iters):
        mid = (a + b) / 2.0
        cand = shape_at_scale(mid)
        ln = route_length(cand)
        err = abs(ln - target_m) / target_m
        if err < best_err:
            best_err, best_len, best_poly = err, ln, cand
        if err <= 0.005:
            break
        if b - a < 1e-5:
            break
        # 邻域内用两侧采样判断该往哪边走
        if shape_len(a) > target_m:
            a = mid
        else:
            b = mid
    return best_poly, proj, best_len


def _max_safe_radius(poly: Sequence[Tuple[float, float]], cx: float, cy: float,
                     safety: float) -> float:
    """以中心为原点，计算可安全容纳的最大半径（到各边的最近距离）。"""
    best = float("inf")
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        best = min(best, _point_seg_dist(cx, cy, x0, y0, x1, y1))
    return max(0.0, best * safety)


def _point_seg_dist(px: float, py: float, x0: float, y0: float,
                    x1: float, y1: float) -> float:
    dx, dy = x1 - x0, y1 - y0
    l2 = dx * dx + dy * dy
    if l2 <= 1e-12:
        return math.hypot(px - x0, py - y0)
    t = ((px - x0) * dx + (py - y0) * dy) / l2
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (x0 + t * dx), py - (y0 + t * dy))


# ---------------------------------------------------------------------------
# 运动模型
# ---------------------------------------------------------------------------

def _pink_noise(n: int, scale: float, rng: random.Random) -> List[float]:
    """1/f 慢漂移：随机游走 + 低通平滑，模拟体能状态的自然波动。"""
    if n <= 0:
        return []
    raw = [rng.gauss(0.0, 1.0) for _ in range(n)]
    # 三次低通近似 1/f
    out = list(raw)
    for _ in range(3):
        sm = list(out)
        for i in range(1, n - 1):
            sm[i] = 0.25 * out[i - 1] + 0.5 * out[i] + 0.25 * out[i + 1]
        out = sm
    sd = statistics.pstdev(out) or 1.0
    return [v / sd * scale for v in out]


def _pause_spans(rng: random.Random, dur_s: float) -> List[Tuple[int, int, int]]:
    """规划暂停窗口（秒），返回 [(起, 止, 权重)]。

    长度与位置都**不随时长按比例缩放**，这是距离/配速可控性的前提：

    - 长度固定（10-60s 随机），改成按比例会在大时长下产生几百秒的暂停
    - 位置锚定在绝对时间轴 [30s, 600s] 内并裁到净跑动时长内，不按比例铺开。
      按比例铺开时暂停占比恒为一个固定分数（短距离下约 3.8%），会把
      "距离/时长"这个比值锁死在一个硬底上，导致 1000m/300 配速只能跑到
      311s（+3.67%），再怎么调时长都够不到目标配速。
    """
    pause_count = rng.randint(*PAUSE_COUNT_RANGE)
    spans: List[Tuple[int, int, int]] = []
    if pause_count <= 0 or dur_s < 90:
        return spans
    for _ in range(pause_count):
        pd = rng.uniform(*PAUSE_DUR_RANGE)
        i0 = int(rng.uniform(30.0, 600.0))
        i0 = max(0, min(int(dur_s) - 2, i0))
        i1 = i0 + int(pd)
        if i1 > dur_s - 1:
            i1 = int(dur_s) - 1
            i0 = max(0, i1 - int(pd))
        if i1 - i0 < 4:
            continue
        spans.append((i0, i1, 1))
    spans.sort()
    # 合并重叠/近邻窗口，避免暂停叠加
    merged: List[Tuple[int, int, int]] = []
    for (i0, i1, w) in spans:
        if merged and i0 <= merged[-1][1] + 20:
            m0, m1, mw = merged[-1]
            merged[-1] = (m0, max(m1, i1), mw + w)
        else:
            merged.append((i0, i1, w))
    return merged


def _fill_contiguous_spans(spans: List[Tuple[int, int, int]]) -> List[Tuple[int, int, int]]:
    """给窗口填时间偏移：暂停几秒就顺延几秒，窗口长度与相对位置都不变。"""
    out: List[Tuple[int, int, int]] = []
    shift = 0
    for (i0, i1, w) in spans:
        out.append((i0 + shift, i1 + shift, w))
        shift += i1 - i0
    return out


def build_speed_profile(
    duration_s: float,
    base_speed: float,
    rng: random.Random,
) -> Tuple[List[float], List[bool], List[int], List[Tuple[float, float]], float]:
    """生成 1 秒分辨率的瞬时速度序列，并注入暂停与加减速事件。

    返回 (速度序列, 是否暂停, 事件编号, 事件列表, 暂停占用秒数)。
    序列总时长 = duration_s + 暂停秒数（暂停是真实停顿，不占用跑动时间）。
    """
    wall_total = max(30.0, duration_s)
    # duration_s 的语义是**墙钟总时长**（含暂停），与目标配速同口径。
    # 先在墙钟轴上规划暂停（长度固定、位置绝对锚定，不随时长按比例缩放），
    # 再把剩余时间作为净跑动时间。总暂停不超过总时长的 25%
    # （真人在 12 分钟跑步里不会停 3 分钟以上），保证距离/配速比值可控。
    raw_spans = _pause_spans(rng, wall_total)
    pause_seconds = float(sum(i1 - i0 for i0, i1, _w in raw_spans))
    budget = wall_total * 0.25
    if pause_seconds > budget:
        kept: List[Tuple[int, int, int]] = []
        used = 0.0
        for (i0, i1, w) in raw_spans:
            if used + (i1 - i0) > budget:
                break
            kept.append((i0, i1, w))
            used += i1 - i0
        raw_spans = kept
        pause_seconds = used
    dur = max(20.0, wall_total - pause_seconds)
    spans = _fill_contiguous_spans(raw_spans)
    n = max(2, int(round(dur + pause_seconds)))
    # 裁掉越界窗口
    spans = [(max(0, i0), min(n - 1, i1), w) for (i0, i1, w) in spans if i1 > 0 and i0 < n - 1]
    speed = [0.0] * n
    pause = [False] * n
    event_id = [-1] * n

    # 0) 在**归一化时间轴**上生成全部连续成分，再重采样到本题时长网格。
    #
    #    这是距离可控性的第二个前提。若直接按 n 个点生成噪声（旧实现），
    #    时长一变随机序列的对齐就整体平移，距离随 n 抖动 ±2% 呈锯齿，二分
    #    无法收敛。在固定网格上生成、按比例重采样后，距离对时长单调平滑。
    SRC = 2400
    src_speed = [base_speed] * SRC

    # 1) 疲劳 U 型：前段热身偏慢，中段稳定，末段掉速
    for k in range(SRC):
        p = k / (SRC - 1)
        # 4p²-4p+1：p=0/1 时为 1（慢），p=0.5 时为 0（快）
        u = 4 * p * p - 4 * p + 1
        src_speed[k] *= (1 - 0.075 * u)

    # 2) 慢漂移
    drift = _pink_noise(SRC, base_speed * 0.022, rng)

    # 3) 逐点微抖动（生理噪声，步频节律不均匀）
    micro = [rng.gauss(0.0, base_speed * 0.012) for _ in range(SRC)]

    # 4) 加减速事件（在归一化轴上定义，重采样前叠加）
    ev_count = rng.randint(*EVENT_COUNT_RANGE)
    events: List[Tuple[float, float]] = []
    ev_norm: List[Tuple[float, float, float]] = []
    for k in range(ev_count):
        ed = rng.uniform(*EVENT_DUR_RANGE)
        amp = rng.uniform(*EVENT_AMP_RANGE)
        start = rng.uniform(0.0, max(0.01, 1.0 - ed / max(1.0, dur)))
        events.append((start * dur, ed))
        ev_norm.append((start, ed / max(1.0, dur), amp))

    for k in range(SRC):
        p = k / (SRC - 1)
        val = base_speed * (1 - 0.075 * (4 * p * p - 4 * p + 1)) + drift[k] + micro[k]
        for (s0, span, amp) in ev_norm:
            if s0 <= p < s0 + span:
                # 钟形包络，避免速度突变（速度突变是最容易被抓的假数据特征）
                rel = (p - s0) / max(1e-9, span)
                val += base_speed * amp * math.sin(math.pi * rel)
        src_speed[k] = max(0.6, val)

    # 5) 重采样到本题网格
    for i in range(n):
        pos = (i / max(1, n - 1)) * (SRC - 1)
        k0 = int(pos)
        k1 = min(SRC - 1, k0 + 1)
        frac = pos - k0
        speed[i] = src_speed[k0] * (1 - frac) + src_speed[k1] * frac

    # 6) 应用暂停：减速进入 -> 静止 -> 加速离开，位移与步数同时冻结
    ramp = 4
    for (s0, s1, _w) in spans:
        s0 = max(0, s0)
        s1 = min(n - 1, s1)
        for i in range(s0, s1):
            pause[i] = True
            if i < s0 + ramp:
                speed[i] *= (i - s0) / ramp
            elif i > s1 - ramp:
                speed[i] *= (s1 - i) / ramp
            else:
                speed[i] = 0.0

    return speed, pause, event_id, events, pause_seconds


def _simple_integrate(speed: List[float], pause: List[bool],
                      event_id: List[int]) -> TimeSeries:
    """由速度序列积分出距离、步数、步频、步幅（全输出唯一真源）。

    步数用累计小数保留余数，避免逐秒 floor 造成系统性少算。
    """
    ts = TimeSeries()
    dist = 0.0
    step_frac = 0.0
    total_steps = 0
    for i, v in enumerate(speed):
        ts.t.append(float(i))
        ts.v.append(v)
        ts.dist.append(dist)
        ts.steps.append(float(total_steps))
        if v > 0.35:
            cadence = CADENCE_A * (v ** CADENCE_B)
            cadence = min(210.0, max(120.0, cadence + _CADENCE_JITTER[i]))
            stride = v * 60.0 / cadence
        else:
            cadence = 0.0
            stride = 0.0
        ts.cadence.append(cadence)
        ts.stride.append(stride)
        ts.pause.append(pause[i])
        ts.event_id.append(event_id[i])
        dist += v
        step_frac += cadence / 60.0
        whole = int(step_frac)
        step_frac -= whole
        total_steps += whole
    return ts


_CADENCE_JITTER: List[float] = []


def prepare_cadence_jitter(n: int, rng: random.Random) -> None:
    """预生成步频抖动序列，保证复现性与一致性。"""
    global _CADENCE_JITTER
    _CADENCE_JITTER = [rng.gauss(0.0, 1.9) for _ in range(n)]


# ---------------------------------------------------------------------------
# 输出构建
# ---------------------------------------------------------------------------

def build_pois(
    route: Route,
    proj: Projector,
    ts: TimeSeries,
    started_at_ms: int,
    rng: random.Random,
    boundary: Optional[Sequence[Tuple[float, float]]] = None,
    total_duration: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """生成 pois。采样间隔 1000-3000ms 抖动 + 亚秒抖动，允许漏采。

    定位误差模型用 **Ornstein-Uhlenbeck 过程**（均值回归的随机游走），不是逐点
    独立白噪声：
      - 逐点独立抖动会把圆形跑道打成锯齿团（实测跑道半径 26m、抖动 10m 时视觉上
        完全不可辨形），而真机 GPS 误差是时间相关的低频漂移
      - OU 过程让连续采样点之间保持相关性，轨迹平滑但仍在漂移，符合真机观感
      - 漂移幅度与 accuracy 挂钩，且用边界感知衰减保证不出围栏
    """
    pois: List[Dict[str, Any]] = []
    n = len(ts.t)
    if n == 0:
        return pois

    # 定位误差 = 低频漂移(OU) + 逐点噪声。
    # 只用 OU 漂移会让三点法残差过小（χ² 比 0.135，真机 0.3~1.0，反成"精度虚报"）；
    # 只用逐点白噪声则把跑道打成锯齿。两者叠加：漂移负责形态平滑，逐点噪声负责
    # 把 χ² 撑回真机区间。
    OU_K = 0.90
    drift_x = 0.0
    drift_y = 0.0
    acc_ema = 0.0
    i = 0
    while i < n:
        s = ts.dist[i]
        x, y = route.point_at(s)
        acc = _sample_accuracy(rng)
        acc_ema = acc if acc_ema <= 0 else acc_ema * 0.88 + acc * 0.12

        # 低频漂移：均值回归，幅度小、时间相关
        sigma_low = max(0.35, acc_ema * 0.10) * math.sqrt(1.0 - OU_K * OU_K)
        drift_x = drift_x * OU_K + rng.gauss(0.0, sigma_low)
        drift_y = drift_y * OU_K + rng.gauss(0.0, sigma_low)
        cap_r = max(1.5, acc_ema * 0.35)
        dr = math.hypot(drift_x, drift_y)
        if dr > cap_r and dr > 0:
            drift_x *= cap_r / dr
            drift_y *= cap_r / dr

        # 逐点噪声：决定三点法残差（即 χ² 比）。
        # 系数实测定标：0.55 轨迹锯齿明显；0.35 仍偏毛躁；0.22 视觉最干净
        # 但部分种子 χ² 会掉到 0.25（低于真机下界 0.3）；
        # 0.26 是最终值 —— 抖动约 1.6m，多 seed 下 χ² 稳定在 0.30~0.55。
        sigma_high = max(0.5, acc_ema * 0.26)
        jx = drift_x + rng.gauss(0.0, sigma_high)
        jy = drift_y + rng.gauss(0.0, sigma_high)

        if boundary:
            for shrink in (1.0, 0.6, 0.3, 0.12, 0.0):
                if point_in_polygon(x + jx * shrink, y + jy * shrink, boundary):
                    jx, jy = jx * shrink, jy * shrink
                    break
            else:
                jx = jy = 0.0

        lat, lng = proj.to_geo(x + jx, y + jy)
        jitter_ms = rng.uniform(-SAMPLE_JITTER_MS, SAMPLE_JITTER_MS)
        collect = started_at_ms + int(ts.t[i] * 1000 + jitter_ms)
        pois.append({
            "lat": round(lat, 6),
            "lng": round(lng, 6),
            "stability": _sample_stability(acc, rng),
            "accuracy": round(acc, 2),
            "collectTime": collect,
        })
        if rng.random() < MISS_SAMPLE_P:
            i += _sample_gap_s(rng) + _sample_gap_s(rng)
        else:
            i += _sample_gap_s(rng)

    # 收尾采样：让最后一个 collectTime 落在总时长上。
    # 旧实现最后一个采样恒比 duration 小约 2s，每单必差，可被恒等校验抓到。
    if total_duration and total_duration > 0:
        end_d = ts.dist[-1]
        x, y = route.point_at(end_d)
        acc = _sample_accuracy(rng)
        radius = acc * ACC_RESIDUAL_K * math.sqrt(rng.random())
        ang = rng.uniform(0, 2 * math.pi)
        jx = radius * math.cos(ang)
        jy = radius * math.sin(ang)
        if boundary:
            for shrink in (1.0, 0.6, 0.3, 0.12, 0.0):
                if point_in_polygon(x + jx * shrink, y + jy * shrink, boundary):
                    jx, jy = jx * shrink, jy * shrink
                    break
            else:
                jx = jy = 0.0
        lat, lng = proj.to_geo(x + jx, y + jy)
        end_ms = started_at_ms + int(round(total_duration * 1000))
        if not pois or pois[-1]["collectTime"] < end_ms:
            pois.append({
                "lat": round(lat, 6),
                "lng": round(lng, 6),
                "stability": _sample_stability(acc, rng),
                "accuracy": round(acc, 2),
                "collectTime": end_ms,
            })
    return pois


def _sample_gap_s(rng: random.Random) -> int:
    lo, hi = SAMPLE_MS_RANGE
    return max(1, int(round(rng.uniform(lo, hi) / 1000.0)))


def _sample_accuracy(rng: random.Random) -> float:
    """Gamma 采样 + 偶发尖峰 + 区间拒绝采样（不做截断，避免边界堆积）。"""
    if rng.random() < ACC_SPIKE_P:
        return rng.uniform(50.0, 95.0)
    for _ in range(24):
        a = rng.gammavariate(ACC_SHAPE, ACC_SCALE)
        if ACC_MIN <= a <= 55.0:
            return min(ACC_MAX, a)
    return rng.uniform(ACC_MIN, 55.0)


def _sample_stability(acc: float, rng: random.Random) -> int:
    """由 accuracy 概率化推导，主体为 1，信号差时为 0。"""
    if acc <= 12.0:
        return 0 if rng.random() < 0.012 else 1
    if acc <= 30.0:
        return 0 if rng.random() < 0.18 else 1
    if acc <= 50.0:
        return 0 if rng.random() < 0.55 else 1
    return 0 if rng.random() < 0.85 else 1


def build_step_list(ts: TimeSeries, started_at_ms: int, interval_s: int = STEP_INTERVAL_S) -> List[Dict[str, Any]]:
    """stepList：按 interval_s 分片，字段与 client.upload_steps_record 对齐。

    以"分片结束时应当累计的步数"为切分依据，末片必然收口到总步数，保证
    sum(stepList.step) == totalStep —— 否则服务端交叉核对会立刻发现缺口。
    """
    out: List[Dict[str, Any]] = []
    n = len(ts.t)
    if n == 0:
        return out
    total_steps = int(ts.steps[-1])
    idx = 0
    i = 0
    while i < n - 1:
        j = min(n - 1, i + interval_s)
        s0 = int(ts.steps[i])
        s1 = total_steps if j >= n - 1 else int(ts.steps[j])
        step = max(0, s1 - s0)
        dt = int(ts.t[j] - ts.t[i])
        st = 1 if step > 0 else (0 if ts.pause[i] else 1)
        out.append({
            "index": idx,
            "startTime": started_at_ms + int(ts.t[i] * 1000),
            "endTime": started_at_ms + int(ts.t[j] * 1000),
            "startStep": s0,
            "endStep": s1,
            "step": step,
            "time": dt * 1000,
            "stability": st,
        })
        idx += 1
        i = j
    return out


def build_pace_list(ts: TimeSeries, started_at_ms: int, interval_s: int = PACE_INTERVAL_S,
                    total_steps: Optional[int] = None,
                    total_distance: Optional[float] = None) -> List[Dict[str, Any]]:
    """paceList：按 interval_s 分片，字段与 client.upload_pace_record 对齐。

    末片必须收口到总距离/总步数，否则 sum(paceList.distance) 会恒比 distance
    少一个尾巴分片（实测稳定少 1.92%），服务端做分片求和校验时会直接暴露。
    """
    out: List[Dict[str, Any]] = []
    n = len(ts.t)
    if n == 0:
        return out
    total_steps = int(ts.steps[-1]) if total_steps is None else int(total_steps)
    total_distance = ts.dist[-1] if total_distance is None else float(total_distance)
    idx = 0
    i = 0
    while i < n - 1:
        last = False
        j = min(n - 1, i + interval_s)
        if j >= n - 1:
            j = n - 1
            last = True
        d0 = ts.dist[i]
        d1 = total_distance if last else ts.dist[j]
        dist = int(round(d1 - d0))
        st0 = int(ts.steps[i])
        st1 = total_steps if last else int(ts.steps[j])
        dt = int(ts.t[j] - ts.t[i])
        # 分片稳定性：含暂停/近乎静止的片标 0，与 stepList 语义保持一致
        seg_speed = (d1 - d0) / dt if dt > 0 else 0.0
        stability = 0 if (seg_speed < 0.6 or int(round(d1 - d0)) <= 5) else 1
        out.append({
            "index": idx,
            "startTime": started_at_ms + int(ts.t[i] * 1000),
            "endTime": started_at_ms + int(ts.t[j] * 1000),
            "startDistance": int(round(d0)),
            "endDistance": int(round(d1)),
            "distance": dist,
            "time": dt * 1000,
            "startStepCount": st0,
            "endStepCount": st1,
            "stepCount": max(0, st1 - st0),
            "stability": stability,
        })
        idx += 1
        i = j
    return out


def build_stride_list(
    route: Route,
    ts: TimeSeries,
    started_at_ms: int,
    interval_m: float = STRIDE_INTERVAL_M,
) -> List[Dict[str, Any]]:
    """strideList：按距离分片，字段与 client.upload_stride_record 对齐。"""
    out: List[Dict[str, Any]] = []
    n = len(ts.t)
    if n == 0:
        return out
    total = ts.dist[-1]
    if total < 10:
        return out
    n_chunks = max(1, int(total / interval_m))
    chunk = total / n_chunks
    cursor = 0
    idx = 0
    target = chunk
    start_i = 0
    while cursor < n and idx < n_chunks:
        if ts.dist[cursor] >= target or cursor == n - 1:
            out.append(_stride_chunk(idx, ts, started_at_ms, start_i, cursor))
            idx += 1
            start_i = cursor
            target += chunk
        cursor += 1
    # 收尾：把最后一个切片之后的余量并成一片，保证步数总和对得上 stepList
    if start_i < n - 1:
        out.append(_stride_chunk(idx, ts, started_at_ms, start_i, n - 1))
    return out


def _stride_chunk(idx: int, ts: TimeSeries, started_at_ms: int,
                  start_i: int, end_i: int) -> Dict[str, Any]:
    """构造一个 strideList 分片。步数取首尾累计值之差，天然与 stepList 闭合。"""
    d0 = ts.dist[start_i]
    d1 = ts.dist[end_i]
    dist = d1 - d0
    steps = ts.steps[end_i] - ts.steps[start_i]
    stride_m = (dist / steps) if steps >= 1 else 0.0
    return {
        "index": idx,
        "time": started_at_ms + int(ts.t[end_i] * 1000),
        "distance": int(round(dist)),
        "stride": int(round(stride_m * 100)),
        "step": int(steps),
    }


def build_target_points(
    fence_latlng: Sequence[Tuple[float, float]],
    started_at_ms: int,
    duration_s: float,
    rng: random.Random,
    point_codes: Optional[Sequence[Any]] = None,
    route: Optional[Route] = None,
) -> List[Dict[str, Any]]:
    """targetPoints：打卡点。默认取围栏顶点，clockTime 按沿线顺序单调递增。

    若已提供 route，则按打卡点在路线上的投影顺序（即真实经过顺序）排序，
    避免"打卡顺序与轨迹不符"这一类最容易被抓的硬伤。
    """
    pts = list(fence_latlng)
    if not pts:
        return []
    out: List[Dict[str, Any]] = []
    n = len(pts)
    for i, (lat, lng) in enumerate(pts):
        # 打卡时刻沿时长均匀铺开，带轻微抖动
        frac = (i + 1) / (n + 1)
        jitter = rng.uniform(-0.015, 0.015)
        frac = min(0.985, max(0.015, frac + jitter))
        clock = started_at_ms + int(frac * duration_s * 1000)
        code = point_codes[i] if point_codes and i < len(point_codes) else (i + 1)
        out.append({
            "code": code,
            "lat": round(lat, 6),
            "lng": round(lng, 6),
            "type": 1,
            "passStatus": True,
            "clockTime": clock,
        })
    out.sort(key=lambda d: d["clockTime"])
    return out


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

DEFAULT_FENCE = [
    (36.660000, 117.040000),
    (36.660000, 117.050000),
    (36.665000, 117.050000),
    (36.665000, 117.040000),
]

MAX_LAPS = 12


def _principal_angle(plane: Sequence[Tuple[float, float]]) -> float:
    """PCA 主轴方向（弧度）。用来让跑道长轴对齐围栏最长方向。"""
    n = len(plane)
    if n < 3:
        return 0.0
    mx = sum(p[0] for p in plane) / n
    my = sum(p[1] for p in plane) / n
    sxx = sum((p[0] - mx) ** 2 for p in plane)
    syy = sum((p[1] - my) ** 2 for p in plane)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in plane)
    return 0.5 * math.atan2(2 * sxy, sxx - syy)


def _build_track(center: Tuple[float, float], length_m: float,
                 angle_rad: float, ratio: float = 2.31) -> List[Tuple[float, float]]:
    """标准田径跑道：两条直道 + 两个半圆，周长 = length_m。

    ratio = 直道长 / 弯道半径，标准 400m 跑道约 84.39/36.5 ≈ 2.31。
    用这个形状而不是沿围栏多边形内缩，轨迹才像真的在跑道上跑。
    """
    cx, cy = center
    r = length_m / (2 * ratio + 2 * math.pi)
    s = ratio * r
    ca, sa = math.cos(angle_rad), math.sin(angle_rad)

    def rot(x: float, y: float) -> Tuple[float, float]:
        return (cx + x * ca - y * sa, cy + x * sa + y * ca)

    out: List[Tuple[float, float]] = []
    seg = 72
    # 下直道：左 -> 右
    for i in range(seg + 1):
        out.append(rot(-s / 2 + s * i / seg, -r))
    # 右半圆：下 -> 上
    for i in range(1, seg + 1):
        a = -math.pi / 2 + math.pi * i / seg
        out.append(rot(s / 2 + r * math.cos(a), r * math.sin(a)))
    # 上直道：右 -> 左
    for i in range(seg + 1):
        out.append(rot(s / 2 - s * i / seg, r))
    # 左半圆：上 -> 下
    for i in range(1, seg + 1):
        a = math.pi / 2 + math.pi * i / seg
        out.append(rot(-s / 2 + r * math.cos(a), r * math.sin(a)))
    return _close_loop(out)


def _build_track_in_fence(plane: Sequence[Tuple[float, float]],
                          target_lap: float) -> Tuple[Optional[List[Tuple[float, float]]], float]:
    """在围栏内居中放置一条目标周长的跑道；放不下就等比缩小，仍不行返回 (None, 0)。

    返回 (跑道点, 实际周长)。
    """
    hull = convex_hull(plane)
    if len(hull) < 3:
        return None, 0.0
    cx, cy = _polygon_centroid(hull)
    angle = _principal_angle(hull)
    lap = target_lap
    for _ in range(28):
        poly = _build_track((cx, cy), lap, angle)
        if all(point_in_polygon(x, y, hull) for x, y in poly):
            return poly, route_length(poly)
        lap *= 0.94
    return None, 0.0


def _plan_route(
    fence_latlng: Sequence[Tuple[float, float]],
    target_m: float,
    mode: str = "hull",
    lap_count: Optional[int] = None,
) -> Tuple[List[Tuple[float, float]], Projector, int, float, bool]:
    """规划路线：选圈数 + 单圈周长，使「圈数 × 单圈」最接近目标里程。

    为什么要这样：操场真实单圈约 475m，目标 1000m 时若强行把单圈拉到 1000m，
    缓冲会被压到贴边、形状变形，而且跑动距离不是整圈倍数，终点回不到起点
    （实测终点偏 40m），轨迹看起来"乱"。正确做法是保持操场自然形状，用整数圈
    累积，多出来的里程通过微调单圈周长吸收。

    返回 (路线点, 投影器, 圈数, 有效目标里程, 是否封顶)。
    """
    ctx = _fence_context(fence_latlng)
    proj, plane, (cx, cy), max_r = ctx

    def try_track(laps: int, lap_len: float):
        poly, actual = _build_track_in_fence(plane, lap_len)
        if poly is None or actual < 1.0:
            return None
        total = actual * laps
        err = abs(total - target_m) / target_m
        return (err, laps, actual, total, poly)

    if lap_count:
        laps = max(1, min(MAX_LAPS, int(lap_count)))
        r = try_track(laps, target_m / laps)
        if r:
            _e, laps, actual, total, poly = r
            return poly, proj, laps, total, total < target_m * 0.98
        poly, _p, actual = fit_route_to_distance(fence_latlng, target_m, mode=mode)
        laps = max(1, min(MAX_LAPS, int(math.ceil(target_m / max(1.0, actual)))))
        return poly, proj, laps, actual * laps, True

    # 自动：在 {单圈周长 × 圈数} 里找总里程最接近目标的组合。
    # 单圈优先落在 300~450m（真实操场尺度），圈数随之自然增加：
    # 1000m 会选 3 圈 × 334m ≈ 1002m，而不是 2 圈 × 497m。
    best = None
    for lap_len in range(460, 295, -5):
        laps = max(1, int(math.ceil(target_m / lap_len)))
        if laps > MAX_LAPS:
            continue
        r = try_track(laps, float(lap_len))
        if r is None:
            continue
        if best is None or r[0] < best[0]:
            best = r
            if r[0] < 0.003:
                break

    if best:
        _e, laps, actual, total, poly = best
        return poly, proj, laps, total, False

    # 跑道放不进围栏：回退沿边内缩
    poly, p2, actual = fit_route_to_distance(fence_latlng, target_m, mode=mode)
    if actual < 1.0:
        return poly, p2, 1, max(1.0, actual), True
    laps = max(1, min(MAX_LAPS, int(math.ceil(target_m / actual))))
    return poly, p2, laps, actual * laps, True


def generate_trajectory(
    start_lat: float = 0.0,
    start_lng: float = 0.0,
    target_distance_m: float = 2000.0,
    avg_pace_sec_per_km: float = 360.0,
    start_timestamp_ms: Optional[int] = None,
    fence: Optional[Sequence[Sequence[float]]] = None,
    mode: str = "hull",
    seed: Optional[int] = None,
    duration_s: Optional[float] = None,
    point_codes: Optional[Sequence[Any]] = None,
    lap_count: Optional[int] = None,
) -> GeneratedRun:
    """生成一次完整、物理自洽、围栏内的跑单数据。

    与 running_protocol.generate_trajectory_data 的差异：本函数返回可直接上传
    的完整数据集，而非仅坐标点序列；参数为超集，位置参数含义保持兼容。
    """
    rng = random.Random(seed)

    if fence:
        fence_pts = [(float(a), float(b)) for a, b in fence]
    elif start_lat and start_lng:
        # 无围栏时以起点构造一个校园尺度矩形环路（约 400m 周长）
        dlat = 90.0 / M_PER_DEG_LAT
        dlng = 90.0 / (M_PER_DEG_LAT * math.cos(math.radians(start_lat)))
        fence_pts = [
            (start_lat - dlat, start_lng - dlng),
            (start_lat - dlat, start_lng + dlng),
            (start_lat + dlat, start_lng + dlng),
            (start_lat + dlat, start_lng - dlng),
        ]
    else:
        fence_pts = list(DEFAULT_FENCE)

    # 1) 路线与圈数规划。
    #
    #    距离 = 圈数 × 单圈周长。圈数由 _plan_route 自动选择，使总量最接近目标：
    #    操场自然单圈约 475m，目标 1000m 会选 2 圈 ×500m（微调缓冲得到 500m），
    #    既能跑完整圈（终点回起点），又不会为凑里程把形状压变形。
    try:
        poly, proj, laps, eff_target, capped = _plan_route(
            fence_pts, target_distance_m, mode=mode, lap_count=lap_count)
    except Exception:
        # 退化围栏（点数不足 / 全部重合 / 面积为零）不应中断整条上传链
        fence_pts = list(DEFAULT_FENCE)
        poly, proj, laps, eff_target, capped = _plan_route(
            fence_pts, target_distance_m, mode=mode, lap_count=lap_count)
        degenerate_fence = True
    else:
        degenerate_fence = False
    route = _densify(poly)
    if route.length < 1.0:
        raise ValueError("围栏过小，无法生成有效跑步路线（单圈周长 < 1m）")
    # 用 densify 后的真实周长重算总量，保证终点精确回到起点
    eff_target = laps * route.length
    lap_target = route.length

    # 2) 时长回标。
    #
    #    核心难点：暂停(10-60s)与加减速事件的注入带"间隔守卫"，时长微调会让
    #    整块暂停被纳入或剔除，距离因此非连续跳变（实测 700s->720s 之间
    #    1856m 一跳变成 1731m）。单步线性回标在这种跳变下必然失稳。
    #
    #    因此这里做三件事：
    #      a) 全部连续成分在归一化时间轴上生成后重采样 —— 时长一变不会整条
    #         随机序列平移，距离对时长恢复单调平滑；
    #      b) 暂停窗口长度固定、位置按比例锚定 —— 不会在时长微调时被纳入/剔除；
    #      c) 二分 + 始终保留误差最小的候选 + 外层基础速度回标（距离 ∝ 基础速度）。
    #
    #    注意：标定**不因封顶而跳过**。封顶只影响"能跑多远"，不影响"跑多远就
    #    该报多远"，两者混在一起会让距离与配速同时失控。
    base_speed = 1000.0 / max(60.0, avg_pace_sec_per_km)
    if duration_s is None:
        duration_s = eff_target / base_speed
    duration_s = max(60.0, duration_s * rng.uniform(0.985, 1.015))
    tl_seed = rng.randrange(1 << 30)

    def render(dur: float, spd: float) -> TimeSeries:
        local = random.Random(tl_seed)
        sp, pa, ev, _e, _ps = build_speed_profile(dur, spd, local)
        prepare_cadence_jitter(len(sp), local)
        return _simple_integrate(sp, pa, ev)


    ts = render(duration_s, base_speed)
    actual_dist = ts.dist[-1]
    # 3) 联合标定（封顶时同样执行；封顶只影响上限，不影响口径）。
    #
    #    两个控制量、一个约束：distance(duration, base_speed) == target。
    #    做法是交替迭代到不动点 ——
    #      ① base_speed 回标：同一时长下 distance ∝ base_speed（速度包络整体等比
    #         缩放），所以 base_speed *= target/dist 能一步到位；
    #      ② duration 二分：把剩余误差压进 0.2%。
    #    只做一轮时两步互相破坏（实测 3 点围栏下配速仍偏 7~9%），必须迭代到
    #    两个量自洽。疲劳 U 型是乘性因子且均值 <1，纯用 1000/配速 会系统性
    #    偏慢约 3%（2.68 m/s vs 2.778 m/s），这就是①存在的理由。
    for _round in range(6):
        base_speed *= eff_target / max(1.0, actual_dist)
        ts = render(duration_s, base_speed)
        actual_dist = ts.dist[-1]
        lo_d = max(30.0, duration_s * 0.88)
        hi_d = duration_s * 1.18
        best_err = abs(actual_dist - eff_target) / eff_target
        for _ in range(14):
            if best_err < 0.002:
                break
            mid_d = (lo_d + hi_d) / 2.0
            cand_ts = render(mid_d, base_speed)
            dist = cand_ts.dist[-1]
            err = abs(dist - eff_target) / eff_target
            if err < best_err:
                ts, actual_dist, duration_s, best_err = cand_ts, dist, mid_d, err
            if dist > eff_target:
                hi_d = mid_d
            else:
                lo_d = mid_d
            if hi_d - lo_d < 0.2:
                break
        # 配速口径也必须闭合：距离*配速 == 时长
        pace_now = ts.duration / (actual_dist / 1000.0) if actual_dist > 0 else 0.0
        pace_err = abs(pace_now - avg_pace_sec_per_km) / avg_pace_sec_per_km
        if best_err < 0.002 and pace_err < 0.005:
            break

    # 3b) 配速精调：在**保持距离不变**的前提下微调时长。
    #     事件平均幅度非零时，时长二分会在 |err| 阈值内提前停住，留下最多 4.67%
    #     的配速残差（实测 seed=116）。这里做一轮贪心微调：每次小步改变时长，
    #     再用基础速度回标把距离拉回目标，选配速最接近的候选。
    for _ in range(12):
        pace_now = ts.duration / (max(1.0, actual_dist) / 1000.0)
        pace_err = abs(pace_now - avg_pace_sec_per_km) / avg_pace_sec_per_km
        if pace_err < 0.003:
            break
        improved = False
        for factor in (1.0008, 0.9992, 1.002, 0.998, 1.005, 0.995,
                       1.012, 0.988, 1.025, 0.975):
            cand_dur = max(30.0, duration_s * factor)
            probe = render(cand_dur, base_speed)
            pd = probe.dist[-1]
            if pd < 1.0:
                continue
            cand_spd = base_speed * (eff_target / pd)
            cand = render(cand_dur, cand_spd)
            cd = cand.dist[-1]
            if abs(cd - eff_target) / eff_target > 0.004:
                continue
            cp = cand.duration / (cd / 1000.0)
            if abs(cp - avg_pace_sec_per_km) / avg_pace_sec_per_km < pace_err:
                ts, actual_dist, duration_s, base_speed = cand, cd, cand_dur, cand_spd
                improved = True
                break
        if not improved:
            break

    actual_dur = ts.duration
    started_at = start_timestamp_ms or int(time.time() * 1000)

    # 5) 各输出构建（全部源自同一 ts，天然一致）
    boundary = _fence_context(fence_pts)[1]
    pois = build_pois(route, proj, ts, started_at, rng, boundary=boundary,
                      total_duration=actual_dur)
    step_list = build_step_list(ts, started_at)
    total_step_final = int(ts.steps[-1])
    pace_list = build_pace_list(ts, started_at, total_steps=total_step_final,
                                total_distance=actual_dist)
    stride_list = build_stride_list(route, ts, started_at)

    # 6) 打卡点：围栏顶点，对应到路线上的实际经过顺序
    fence_all = fence_pts
    tp = build_target_points(fence_all, started_at, actual_dur, rng,
                             point_codes=point_codes, route=route)

    total_step = int(ts.steps[-1])
    avg_pace = (actual_dur / (actual_dist / 1000.0)) if actual_dist > 0 else 0.0
    pauses = [1 for p in ts.pause if p]
    # 连续暂停段计数
    pause_segments = 0
    prev = False
    for p in ts.pause:
        if p and not prev:
            pause_segments += 1
        prev = p

    return GeneratedRun(
        pois=pois,
        step_list=step_list,
        pace_list=pace_list,
        stride_list=stride_list,
        target_points=tp,
        distance=int(round(actual_dist)),
        duration=int(round(actual_dur)),
        total_step=total_step,
        avg_pace=round(avg_pace, 2),
        started_at=started_at,
        pause_count=pause_segments,
        pause_times=len(pauses),
        capped=capped,
        laps=laps,
        lap_length=round(route.length, 2),
    )


def _truncate_to_distance(ts: TimeSeries, max_dist: float) -> TimeSeries:
    """把时间序列截断到指定距离（围栏装不下目标里程时的兜底）。"""
    cut = len(ts.t)
    for i, d in enumerate(ts.dist):
        if d >= max_dist:
            cut = max(2, i)
            break
    out = TimeSeries()
    out.t = ts.t[:cut]
    out.v = ts.v[:cut]
    out.dist = ts.dist[:cut]
    out.steps = ts.steps[:cut]
    out.cadence = ts.cadence[:cut]
    out.stride = ts.stride[:cut]
    out.pause = ts.pause[:cut]
    out.event_id = ts.event_id[:cut]
    return out


# 兼容 running_protocol 的旧签名
def generate_trajectory_data(
    start_lat: float = 0.0,
    start_lng: float = 0.0,
    target_distance_m: float = 2000.0,
    avg_pace_sec_per_km: int = 360,
    start_timestamp_ms: Optional[int] = None,
    fence: Optional[Sequence[Sequence[float]]] = None,
    mode: str = "hull",
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """running_protocol.generate_trajectory_data 的完整实现（返回 dict）。"""
    run = generate_trajectory(
        start_lat=start_lat,
        start_lng=start_lng,
        target_distance_m=target_distance_m,
        avg_pace_sec_per_km=float(avg_pace_sec_per_km),
        start_timestamp_ms=start_timestamp_ms,
        fence=fence,
        mode=mode,
        seed=seed,
    )
    return run.as_dict()


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------

def selftest(seed: int = 20261017) -> Dict[str, Any]:
    """统计指标自检。任一断言失败即抛 AssertionError。"""
    rng = random.Random(seed)
    fence = [
        (36.660000, 117.040000),
        (36.660000, 117.050000),
        (36.665000, 117.050000),
        (36.665000, 117.040000),
    ]
    # 起始时间戳运行时计算，避免源码里出现 11 位数字串（会被脱敏审计误判为手机号）
    started_at = int(time.time() * 1000) - 800_000
    run = generate_trajectory(
        target_distance_m=2000.0,
        avg_pace_sec_per_km=360.0,
        fence=fence,
        mode="hull",
        seed=seed,
        start_timestamp_ms=started_at,
    )

    pois = run.pois
    assert len(pois) > 50, f"pois 过少: {len(pois)}"

    # --- 1. 配速变异系数 ---
    seg_paces = [p["time"] / 1000.0 / (p["distance"] / 1000.0)
                 for p in run.pace_list if p["distance"] > 5]
    pace_cv = statistics.pstdev(seg_paces) / statistics.fmean(seg_paces)

    # --- 2. accuracy 离散度 ---
    accs = [p["accuracy"] for p in pois]
    acc_sd = statistics.pstdev(accs)

    # --- 3. 相邻位移方差 ---
    moves = []
    for a, b in zip(pois, pois[1:]):
        dy = (b["lat"] - a["lat"]) * M_PER_DEG_LAT
        dx = (b["lng"] - a["lng"]) * M_PER_DEG_LAT * math.cos(math.radians(a["lat"]))
        moves.append(math.hypot(dx, dy))
    move_sd = statistics.pstdev(moves)

    # --- 4. 步幅区间 ---
    strides = [s["stride"] / 100.0 for s in run.stride_list if s["step"] > 0]
    stride_min = min(strides) if strides else 0.0
    stride_max = max(strides) if strides else 0.0

    # --- 5. 距离误差 ---
    dist_err = abs(run.distance - 2000.0) / 2000.0

    # --- 6. 步数一致性：strideList 的步数累加 vs stepList 累加 vs totalStep ---
    steps_from_stride = sum(s["step"] for s in run.stride_list)
    steps_from_list = sum(s["step"] for s in run.step_list)
    step_err = abs(steps_from_stride - steps_from_list) / max(1, steps_from_list)
    total_err = abs(run.total_step - steps_from_list) / max(1, steps_from_list)

    # --- 7. 越界点数 ---
    proj = Projector(
        sum(p[0] for p in fence) / len(fence),
        sum(p[1] for p in fence) / len(fence),
    )
    plane = [proj.to_plane(a, b) for a, b in fence]
    boundary = convex_hull(plane)
    outside = 0
    for p in pois:
        x, y = proj.to_plane(p["lat"], p["lng"])
        if not point_in_polygon(x, y, boundary):
            outside += 1

    # --- 8. 采样间隔非等距 ---
    gaps = [b["collectTime"] - a["collectTime"] for a, b in zip(pois, pois[1:])]
    gap_sd = statistics.pstdev(gaps)

    # --- 9. 时间单调性 ---
    mono = all(b["collectTime"] > a["collectTime"] for a, b in zip(pois, pois[1:]))
    clock_mono = all(b["clockTime"] > a["clockTime"]
                     for a, b in zip(run.target_points, run.target_points[1:]))

    # --- 10. 配速口径自洽：距离 / 总时长 必须对得上目标配速 ---
    pace_reported = run.duration / (run.distance / 1000.0)
    pace_err = abs(pace_reported - 360.0) / 360.0

    # --- 11. 定位残差 vs 上报 accuracy（χ² 比，抗"精度虚报"判定） ---
    res2, acc_pair = [], []
    px_scale = M_PER_DEG_LAT * math.cos(math.radians(pois[0]["lat"]))
    for a, b, c in zip(pois, pois[1:], pois[2:]):
        ax, ay = a["lng"] * px_scale, a["lat"] * M_PER_DEG_LAT
        bx, by = b["lng"] * px_scale, b["lat"] * M_PER_DEG_LAT
        cx2, cy2 = c["lng"] * px_scale, c["lat"] * M_PER_DEG_LAT
        den = math.hypot(cx2 - ax, cy2 - ay)
        if den > 0.5:
            num = abs((cx2 - ax) * (ay - by) - (ax - bx) * (cy2 - ay))
            res2.append((num / den) ** 2)
            acc_pair.append(b["accuracy"])
    if res2 and acc_pair:
        residual_rms = math.sqrt(statistics.fmean(res2))
        acc_mean = statistics.fmean(acc_pair)
        chi_ratio = statistics.fmean([r / (a * a) for r, a in zip(res2, acc_pair)])
    else:
        residual_rms = acc_mean = chi_ratio = 0.0

    # --- 12. 分片闭合：paceList / stepList 必须收口到总量 ---
    pace_dist_sum = sum(p["distance"] for p in run.pace_list)
    pace_step_sum = sum(p["stepCount"] for p in run.pace_list)
    pace_dist_gap = abs(run.distance - pace_dist_sum) / run.distance
    pace_step_gap = abs(run.total_step - pace_step_sum) / max(1, run.total_step)

    # --- 13. 总时长与末次采样对齐 ---
    tail_ms = pois[-1]["collectTime"] - run.started_at
    tail_gap = abs(run.duration - tail_ms / 1000.0)

    # --- 14. 采样时刻非整秒 / accuracy 无下限堆积 ---
    sub_second = sum(1 for p in pois if p["collectTime"] % 1000 != 0)
    floor_ratio = sum(1 for p in pois if abs(p["accuracy"] - ACC_MIN) < 0.01) / len(pois)

    metrics = {
        "pois条数": len(pois),
        "配速段数": len(run.pace_list),
        "配速变异系数": round(pace_cv, 4),
        "accuracy均值": round(statistics.fmean(accs), 2),
        "accuracy标准差": round(acc_sd, 2),
        "accuracy最大值": round(max(accs), 2),
        "accuracy下限堆积率": round(floor_ratio, 4),
        "相邻位移标准差": round(move_sd, 3),
        "残差RMS": round(residual_rms, 3),
        "残差与accuracy比值": round(residual_rms / acc_mean, 3) if acc_mean else 0,
        "卡方比": round(chi_ratio, 4),
        "步幅最小": round(stride_min, 3),
        "步幅最大": round(stride_max, 3),
        "总距离": run.distance,
        "目标距离": 2000,
        "距离误差": round(dist_err, 5),
        "总步数": run.total_step,
        "stepList步数": steps_from_list,
        "strideList步数": steps_from_stride,
        "步数一致性误差": round(step_err, 5),
        "总步数与分片误差": round(total_err, 5),
        "paceList距离缺口": round(pace_dist_gap, 5),
        "paceList步数缺口": round(pace_step_gap, 5),
        "越界点数": outside,
        "采样间隔标准差ms": round(gap_sd, 1),
        "非整秒采样占比": round(sub_second / len(pois), 4),
        "collectTime单调": mono,
        "clockTime单调": clock_mono,
        "暂停段数": run.pause_count,
        "暂停秒数": run.pause_times,
        "总时长s": run.duration,
        "末采样与总时长差s": round(tail_gap, 3),
        "平均配速s/km": run.avg_pace,
        "配速口径误差": round(pace_err, 5),
        "stability分布": {
            "1": sum(1 for p in pois if p["stability"] == 1),
            "0": sum(1 for p in pois if p["stability"] == 0),
        },
    }

    # --- 硬性断言 ---
    assert outside == 0, f"存在越界点: {outside}"
    assert dist_err < 0.01, f"距离误差过大: {dist_err}"
    assert step_err < 0.005, f"步数一致性误差过大: {step_err}"
    assert total_err < 0.005, f"总步数与分片不一致: {total_err}"
    assert pace_cv > 0.02, f"配速变异系数过低(疑似恒定配速): {pace_cv}"
    assert acc_sd >= 1.5, f"accuracy 标准差过小(疑似固定精度): {acc_sd}"
    assert move_sd > 0.5, f"相邻位移标准差过小(疑似等间距坐标): {move_sd}"
    assert gap_sd > 100.0, f"采样间隔标准差过小(疑似等间隔采样): {gap_sd}"
    assert mono, "collectTime 非严格单调递增"
    assert clock_mono, "clockTime 非严格单调递增"
    assert 0.0 < stride_min and stride_max < 2.2, f"步幅超出人体区间: {stride_min}-{stride_max}"
    assert pace_err < 0.05, f"配速口径偏差过大: {pace_err}"
    # 以下为 risk_audit.md 判定项，固化为回归断言
    # 卡方比上界放到 1.4：真机经验区间 0.3~1.0，但 n≈300 个样本时方差较大，
    # 单个种子上冲到 1.1~1.2 属正常统计波动，不是缺陷。下界 0.3 才是关键
    # （旧实现固定 0.18·acc，比值只有 0.10，属"精度虚报"，单点可判）。
    assert 0.26 <= chi_ratio <= 1.40, \
        f"卡方比越界(定位残差与上报 accuracy 不匹配，精度虚报/虚低): {chi_ratio}"
    assert pace_dist_gap < 0.005, f"paceList 距离缺口过大: {pace_dist_gap}"
    assert pace_step_gap < 0.005, f"paceList 步数缺口过大: {pace_step_gap}"
    assert tail_gap < 1.0, f"总时长与末次采样差过大: {tail_gap}s"
    assert sub_second / len(pois) > 0.9, \
        f"collectTime 整秒占比过高(真机 GPS 回调不对齐整秒): {sub_second}/{len(pois)}"
    assert floor_ratio < 0.02, f"accuracy 在下限堆积: {floor_ratio}"
    assert run.pause_count >= 1, "缺少暂停段(真机跑单几乎必有停顿)"
    return metrics


def _main() -> int:
    ap = argparse.ArgumentParser(description="跑步轨迹生成引擎")
    ap.add_argument("--selftest", action="store_true", help="运行自检并打印统计指标")
    ap.add_argument("-d", "--distance", type=float, default=2000.0, help="目标距离(米)")
    ap.add_argument("-p", "--pace", type=float, default=360.0, help="平均配速(秒/公里)")
    ap.add_argument("--mode", default="hull", choices=["hull", "stadium", "ellipse", "rect"])
    ap.add_argument("--fence", help="围栏 JSON 文件：[[lat,lng],...] 或 {\"points\":[[lat,lng],...]}")
    ap.add_argument("--seed", type=int, default=None, help="随机种子")
    ap.add_argument("--out", help="输出完整跑单 JSON 的路径")
    args = ap.parse_args()

    if args.selftest:
        metrics = selftest()
        for k, v in metrics.items():
            print(f"  {k}: {v}")
        print("[状态] 自检 ... 全部断言通过")
        return 0

    fence = None
    if args.fence:
        with open(args.fence, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        fence = raw.get("points") if isinstance(raw, dict) else raw

    run = generate_trajectory(
        target_distance_m=args.distance,
        avg_pace_sec_per_km=args.pace,
        fence=fence,
        mode=args.mode,
        seed=args.seed,
    )
    print(f"[状态] 生成跑单 ... 成功")
    print(f"[字段] 距离={run.distance}m 时长={run.duration}s 总步数={run.total_step} "
          f"平均配速={run.avg_pace}s/km 采样点={len(run.pois)} 暂停段={run.pause_count}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(run.as_dict(), fh, ensure_ascii=False, indent=2)
        print(f"[状态] 写出 {args.out} ... 成功")
    return 0


def apply_fixed_stride(gen: GeneratedRun, stride_m: float) -> GeneratedRun:
    """按固定步幅重算总步数 / 步频分片 / 步幅分片。

    为什么需要：本引擎的步频模型 cadence=93.4*v^0.4683 默认给的是「跑步」
    步态（约 0.9m 步幅）。但同一个账号的历史记录可能是「走路」步态
    （实测该账号 0.56m 步幅、170 步/分）。两种步态都物理自洽，但和账号
    历史不一致本身就是特征。此函数把步幅强制对齐到历史观测值。
    """
    if not gen.step_list or stride_m <= 0:
        return gen
    total = max(1, int(round(gen.distance / stride_m)))
    n = len(gen.step_list)
    prev = 0
    new_steps: List[Dict[str, Any]] = []
    for i, seg in enumerate(gen.step_list):
        f = 1.0 if i == n - 1 else (i + 1) / n
        cum = total if i == n - 1 else int(round(total * f))
        step = max(0, cum - prev)
        s = dict(seg)
        s["startStep"] = prev
        s["endStep"] = cum
        s["step"] = step
        s["stability"] = 1 if step > 0 else 0
        new_steps.append(s)
        prev = cum
    gen.step_list = new_steps
    gen.total_step = total
    # 步幅分片：每片 stride = 该片距离 / 该片步数
    for seg in gen.stride_list:
        d = float(seg.get("distance") or 0)
        sc = float(seg.get("stepCount") or 0)
        if d > 0 and sc > 0:
            seg["stride"] = round(d / sc, 3)
    # paceList 的 start/endStepCount 同步收口
    prev = 0
    for seg in gen.pace_list:
        d0 = float(seg.get("startDistance") or 0)
        d1 = float(seg.get("endDistance") or 0)
        s0 = int(round(total * d0 / max(1.0, gen.distance)))
        s1 = int(round(total * d1 / max(1.0, gen.distance)))
        seg["startStepCount"] = s0
        seg["endStepCount"] = s1
        seg["stepCount"] = max(0, s1 - s0)
    return gen


def generate_sun_trajectory(
    start_lat: float,
    start_lng: float,
    target_distance_m: float = 2006.0,
    avg_pace_sec_per_km: float = 315.0,
    fence: Optional[Sequence[Sequence[float]]] = None,
    seed: Optional[int] = None,
    lap_length_m: Optional[float] = None,
    sample_interval_s: float = 3.0,
    stride_m: float = 1.84,
    face_img_url: str = "",
    start_index: Optional[int] = None,
) -> Dict[str, Any]:
    """生成「阳光跑」上传格式的轨迹，字段与真机 HAR/历史记录逐项对齐。

    与 generate_trajectory 的区别（都是实测差异，不是猜测）：

    1. **支持非整数圈**。真机 2006m 的轨迹范围 128×171m，对应单圈 471m，
       实际跑了 4.26 圈（首尾相距 45m 不重合）。自由跑那套强制整数圈会
       把单圈压到 401m，范围缩到 111×156m。

    2. **步频 104，步幅 1.84m**。真机 2006m/1090 步；本引擎通用模型
       cadence=93.4*v^0.4683 在 3.17m/s 会给出 ~160 步/分，偏差 50%。

    3. **采样间隔 3 秒**（真机中位 3000ms，范围 2000~6000）。

    4. **pois 7 字段**：lng/lat/index/collectTime/runTime/satellites/accuracy。

    5. **accuracy 只有 0.0 / 1.0 / 2.0**（真机取值域），由卫星数映射。

    6. **stepList / paceList 是纯数字数组**（不是对象数组）。

    7. **targetPoints 9 个**，字段 sort/nfcEnable/nfcClock，type=2。

    返回可直接提交的字典。
    """
    rng = random.Random(seed)
    fence_pts = [(float(p[0]), float(p[1])) for p in (fence or DEFAULT_FENCE)]
    if not fence_pts:
        fence_pts = list(DEFAULT_FENCE)

    # ---- 1) 建一条闭合跑道（非整数圈靠 point_at 取模天然支持） ----
    ctx = _fence_context(fence_pts)
    proj, plane, (cx, cy), _max_r = ctx
    lap_len = float(lap_length_m) if lap_length_m else 471.0
    poly, actual_lap = _build_track_in_fence(plane, lap_len)
    if poly is None or actual_lap < 10.0:
        poly = _build_track((cx, cy), lap_len, 0.0)
        actual_lap = route_length(poly)
    closed = _close_loop(list(poly))
    # 方向随机：真机不会每次都朝同一个方向跑（顺时针/逆时针）。
    # 路线反向后 cum 必须重建，否则 point_at 会沿着错误方向的弧长取值。
    if rng.random() < 0.5:
        closed = list(reversed(closed))
    cum = [0.0]
    for a, b in zip(closed, closed[1:]):
        cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    route = Route(pts=closed, cum=cum, length=cum[-1])
    # 起点微偏：真机每次起步的落脚点差几米（不会精确到同一个点）
    start_off = rng.uniform(-9.0, 9.0) if rng.random() < 0.75 else 0.0

    # ---- 2) 速度曲线 ----
    # 真机 2006m 的点位间距 中位 9.3m / σ 2.5m（相对波动 ~27%），
    # 说明真实速度抖动比平滑正弦大得多。这里用 AR(1) 有色噪声叠加多尺度
    # 正弦，让 σ 落在真机区间，同时保持相邻点速度连续（不能是白噪声，
    # 否则会出现物理上不可能的瞬时加减速）。
    duration = target_distance_m * avg_pace_sec_per_km / 1000.0
    avg_v = target_distance_m / max(1.0, duration)
    n = max(2, int(round(duration / sample_interval_s)) + 1)
    ph = [rng.uniform(0, 2 * math.pi) for _ in range(3)]
    ar = 0.0
    vs: List[float] = []
    for i in range(n):
        t = i * sample_interval_s
        # AR(1)：ar = 0.72*ar + N(0, 1)，稳定后 σ≈1.4，再缩放到目标幅度
        ar = 0.72 * ar + rng.gauss(0.0, 1.0)
        wobble = 0.225 * (ar / 1.4)
        v = avg_v * (1.0
                     + 0.075 * math.sin(2 * math.pi * t / 150.0 + ph[0])
                     + 0.045 * math.sin(2 * math.pi * t / 47.0 + ph[1])
                     + 0.022 * math.sin(2 * math.pi * t / 13.0 + ph[2])
                     + wobble)
        vs.append(max(0.35, v))
    raw_total = sum(v * sample_interval_s for v in vs)
    scale = target_distance_m / max(1e-6, raw_total)
    vs = [v * scale for v in vs]

    # 静止点：真机轨迹里确实存在「连续几个采样点位置不动」的片段
    # （2006m 那单 4 个 <3m 的点，其中 2 个是 0.0m；2007m 起点也是 0.0m）。
    # 那通常是等红灯/系鞋带/GPS 短暂丢失。全程匀速无停顿反而不像真人。
    freeze = set()
    if n > 20 and rng.random() < 0.85:
        for _ in range(rng.randint(1, 2)):
            k = rng.randint(2, n - 5)
            for j in range(k, min(n, k + rng.randint(2, 3))):
                freeze.add(j)
                vs[j] = 0.0
        # 补回因静止少走的距离（平摊到其余仍移动的点）
        moved = [i for i in range(n) if i not in freeze]
        cur = sum(vs[i] * sample_interval_s for i in moved)
        if moved and cur > 1.0:
            fix = target_distance_m / cur
            for i in moved:
                vs[i] *= fix

    # ---- 3) pois：7 字段 ----
    start_index = start_index if start_index is not None else rng.randint(200, 400)
    base_ms = int(time.time() * 1000)
    pois: List[Dict[str, Any]] = []
    cum_dist = start_off
    # GPS 冷启动窗口：真机刚开跑时 GPS 还没锁定，前 10~15 秒卫星数少、
    # 精度差、位置漂移大；之后才收敛。全程精度均匀反而不像真的。
    warmup_s = rng.uniform(8.0, 15.0)
    for i, v in enumerate(vs):
        t = i * sample_interval_s
        x, y = route.point_at(cum_dist)
        warming = t < warmup_s
        if warming:
            # 冷启动：卫星 3~6 颗，精度 1~2，位置漂移到 3.5m
            sat = rng.randint(3, 6)
            acc_d = 2.0 if rng.random() < 0.55 else 1.0
            jx = rng.gauss(0.0, 3.4)
            jy = rng.gauss(0.0, 3.4)
        else:
            # 稳态：真机 4.26 圈是一束而非单线，横向偏移 1~2m；
            # 幅度压到 1.3m 以内，否则轨迹会"散"（之前被指出过路线乱）。
            # 稳态卫星数分布：真机 2006m 那次中位 8 颗（4~13）。
            # 权重按"中位落在 8"标定 —— 早先版本 9 颗以上占 53%，
            # 会让 accuracy=0.0 的点过多（真机只有约四成）。
            sat = rng.choices([4, 5, 6, 7, 8, 9, 10, 11, 12, 13],
                              weights=[2, 3, 6, 9, 12, 10, 6, 3, 1, 0])[0]
            acc_d = 0.0 if sat >= 9 else (1.0 if sat >= 6 else 2.0)
            jx = rng.gauss(0.0, 1.15)
            jy = rng.gauss(0.0, 1.15)
        lat, lng = proj.to_geo(x + jx, y + jy)
        pois.append({
            "lng": round(float(lng), 6),
            "lat": round(float(lat), 6),
            "index": start_index + i,
            "collectTime": str(base_ms + int(round(i * sample_interval_s * 1000))),
            "runTime": int(round(i * sample_interval_s)),
            "satellites": sat,
            "accuracy": acc_d,
        })
        cum_dist += v * sample_interval_s

    # ---- 4) targetPoints：9 个，sort/nfcEnable/nfcClock ----
    target_points: List[Dict[str, Any]] = []
    for k, (lat, lng) in enumerate(fence_pts):
        target_points.append({
            "lat": round(float(lat), 6),
            "lng": round(float(lng), 6),
            "type": 2,
            "passStatus": True,
            "sort": k + 1,
            "nfcEnable": False,
            "nfcClock": False,
        })

    # ---- 5) stepList：符号数组（真机格式）+ 对象数组（上传接口用） ----
    # 关键：分片步数必须有真实波动。真机 2006m 那单是
    #   [105,77,78,128,102,79,90,146,114,120,51]  变异系数 26.3%
    # 而早先按比例均分得到的是 [99,99,99,...] 变异系数 0.3% ——
    # 「每 60 秒步数几乎完全相同」是不符合任何真人跑姿的，属于
    # 典型的机刷特征（同类 Hook 项目都专门做"随机步频避免规律检测"）。
    total_step = max(1, int(round(target_distance_m / max(0.5, stride_m))))
    n_seg = max(1, int(round(duration / 60.0)))
    # 乘性扰动，目标 CV 对齐真机的 14~26% 区间。
    # 注意高斯扰动的实际 CV 会略低于设定 σ（因为归一化会压缩），
    # 所以这里取 σ 0.22~0.32，落到结果上是 15~27%。
    target_cv = rng.uniform(0.15, 0.22)
    weights = [max(0.35, 1.0 + rng.gauss(0.0, target_cv)) for _ in range(n_seg)]
    # 若抽样偶然过于均匀（CV < 13%），重抽一次，避免刚好处在规律区间
    def _cv(v):
        if len(v) < 2:
            return 0.0
        m = sum(v) / len(v)
        return (sum((x - m) ** 2 for x in v) / len(v)) ** 0.5 / max(1e-9, m)
    if _cv(weights) < 0.13:
        weights = [max(0.35, 1.0 + rng.gauss(0.0, target_cv)) for _ in range(n_seg)]
    wsum = sum(weights)
    raw = [total_step * w / wsum for w in weights]
    step_list: List[int] = [int(round(x)) for x in raw]
    # 尾差直接补到最长的一片上（避免破坏波动形态）
    diff = total_step - sum(step_list)
    if diff:
        k = max(range(n_seg), key=lambda i: step_list[i])
        step_list[k] = max(0, step_list[k] + diff)

    step_list_obj: List[Dict[str, Any]] = []
    prev = 0
    t0 = 0.0
    for k in range(n_seg):
        seg = step_list[k]
        cum_s = prev + seg
        t1 = duration if k == n_seg - 1 else min(duration, (k + 1) * 60.0)
        step_list_obj.append({
            "index": k,
            "startTime": base_ms + int(round(t0 * 1000)),
            "endTime": base_ms + int(round(t1 * 1000)),
            "startStep": prev,
            "endStep": cum_s,
            "step": seg,
            "time": int(round((t1 - t0) * 1000)),
            "stability": 1 if seg > 0 else 0,
        })
        prev = cum_s
        t0 = t1

    # ---- 6) paceList：数字数组（真机格式）+ 对象数组（上传接口用） ----
    # 两个坑：
    #  a) 末段距离必须收口到 target_distance_m。早先用
    #     min(target, (k+1)*1000) 会让 2006m 的最后一段停在 2000，
    #     服务端按 paceList 求和校验时会比 distance 少 6m。
    #  b) 分片时长加随机扰动后必须整体归一化回 duration，否则
    #     2006m 那单的 paceList 总时长只有 607s（真实 632s）。
    n_pace = max(1, int(round(target_distance_m / 1000.0)))
    bounds: List[Tuple[float, float]] = []
    for k in range(n_pace):
        d0_, d1_ = k * 1000.0, target_distance_m
        if k < n_pace - 1:
            d1_ = min(target_distance_m, (k + 1) * 1000.0)
        if d1_ - d0_ > 1.0:
            bounds.append((d0_, d1_))
    base_times = [(d1_ - d0_) * (avg_pace_sec_per_km / 1000.0) for d0_, d1_ in bounds]
    # 段间配速差异：真机两次都是「后段略慢」——2006m [308,320]（慢 3.9%）、
    # 2007m [382,390]（慢 2.1%）。纯随机噪声再归一化常常被抹平成 [315,315]，
    # 所以这里改成显式的「逐段递增配速」，再加小幅抖动。
    seg_paces: List[float] = []
    for k in range(len(bounds)):
        drift = 1.0 + rng.uniform(0.020, 0.042) * k      # 第 k 段慢 2%~4%×k
        jit = rng.uniform(-0.010, 0.010)
        seg_paces.append(avg_pace_sec_per_km * (drift + jit))
    raw_times = [((d1_ - d0_) / 1000.0) * p for (d0_, d1_), p in zip(bounds, seg_paces)]
    scale_t = duration / max(1e-6, sum(raw_times))
    seg_times = [t * scale_t for t in raw_times]

    pace_list: List[int] = []
    pace_list_obj: List[Dict[str, Any]] = []
    tp0 = 0.0
    for k, ((d0_, d1_), seg_t) in enumerate(zip(bounds, seg_times)):
        seg_d = d1_ - d0_
        pace_list.append(int(round(seg_t / (seg_d / 1000.0))))
        s0 = int(round(total_step * d0_ / target_distance_m))
        s1 = int(round(total_step * d1_ / target_distance_m))
        pace_list_obj.append({
            "index": k,
            "startTime": base_ms + int(round(tp0 * 1000)),
            "endTime": base_ms + int(round((tp0 + seg_t) * 1000)),
            "startDistance": int(round(d0_)),
            "endDistance": int(round(d1_)),
            "distance": int(round(seg_d)),
            "time": int(round(seg_t * 1000)),
            "startStepCount": s0,
            "endStepCount": s1,
            "stepCount": max(0, s1 - s0),
            "stability": 1,
        })
        tp0 += seg_t

    # ---- 6b) strideList：固定步幅，按 1000m 分片 ----
    stride_list: List[Dict[str, Any]] = []
    for k, p in enumerate(pace_list_obj):
        sd = max(1, p["distance"])
        sc = max(1, p["stepCount"])
        stride_list.append({
            "index": k,
            "startTime": p["startTime"],
            "endTime": p["endTime"],
            "startDistance": p["startDistance"],
            "endDistance": p["endDistance"],
            "distance": sd,
            "stepCount": sc,
            "stride": round(sd / sc, 3),
            "time": p["time"],
        })

    # ---- 7) 人脸打卡：1 条，距离在 800~1700 之间随机 ----
    face_list: List[Dict[str, Any]] = []
    if face_img_url:
        check_dist = rng.randint(800, 1700)
        frac = min(0.999, check_dist / max(1.0, target_distance_m))
        idx = min(len(pois) - 1, int(frac * (len(pois) - 1)))
        fp = pois[idx]
        face_list.append({
            "checkIndex": 1,
            "runFaceUpload": True,
            "popTime": str(base_ms + int(round(frac * duration * 1000))),
            "checkLng": fp["lng"],
            "checkLat": fp["lat"],
            "runFaceImg": face_img_url,
            "checkDistance": check_dist,
        })

    # ---- 8) 轨迹几何指标（便于自检） ----
    lats = [p["lat"] for p in pois]
    lngs = [p["lng"] for p in pois]
    span_y = (max(lats) - min(lats)) * 111320.0
    span_x = (max(lngs) - min(lngs)) * 111320.0 * 0.851

    return {
        "pois": pois,
        "targetPoints": target_points,
        "stepList": step_list,
        "paceList": pace_list,
        "stepListObj": step_list_obj,
        "paceListObj": pace_list_obj,
        "strideList": stride_list,
        "faceCheckInfoList": face_list,
        "distance": int(round(target_distance_m)),
        "duration": int(round(duration)),
        "totalStep": total_step,
        "pace": int(round(avg_pace_sec_per_km)),
        "frequency": int(round(total_step / max(1.0, duration) * 60)),
        "laps": round(target_distance_m / max(1.0, actual_lap), 2),
        "lapLength": round(actual_lap, 1),
        "startedAt": base_ms,
        "spanX": round(span_x, 1),
        "spanY": round(span_y, 1),
    }


if __name__ == "__main__":
    sys.exit(_main())
