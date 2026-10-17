"""滑块缺口识别（轮廓法 + 模板匹配兜底）。

踩过的坑，按时间顺序：

1. 上游用 Canny 边缘 + TM_CCOEFF_NORMED，实测 score 只有 0.17~0.24，
   基本等于随机位置。
2. 改成灰度 + alpha mask + TM_CCORR_NORMED，score 提升到 0.98+，
   但**匹配位置仍会漂移 0~15px** —— 因为缺口区域的图案已被挖空，
   算法匹配到的是"残留纹理"，不同背景下偏移量不同。
3. 最可靠的是**直接找背景图上的白色缺口轮廓**：
   它是个宽高约 42x43 的闭合描边，尺寸与 jigsaw 模板一致。
   用轮廓的 boundingRect 取 x，误差可控制在 ±2px。

坐标语义（来自 HAR 真机样本 x=184.94 / y=15）：
  x = 缺口左边缘 + 10     （上游 int(x-6.5)+16.5 净效果）
  y = 15                  （服务端似乎只校验 x 为主，y 固定即可）
"""
from typing import Optional, Tuple

_MIN_SCORE = 0.45
# 偏移量：0。
# 依据是本项目实测：轮廓法给出 raw=198，而图上白色缺口描边左边缘就在 200，
# 若再加 +10 会推到 208，反而偏右 8px 越过容差。
# 上游的 int(x-6.5)+16.5（净 +10）是针对它自己那批样本校准的，不通用。
X_OFFSET = 0.0
DEFAULT_Y = 15.0


def _decode(b):
    import cv2
    import numpy as np
    return cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_UNCHANGED)


def solve_slider_notch(bg_bytes: bytes, tmpl_bytes: bytes) -> float:
    """识别滑块缺口 x（兼容旧调用）。"""
    x, _y = solve_slider_point(bg_bytes, tmpl_bytes)
    return x


def solve_slider_point(bg_bytes: bytes, tmpl_bytes: bytes) -> Tuple[float, float]:
    """返回 (x, y)：x 为缺口左边缘 + 10，y 固定 15。"""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return 120.0, DEFAULT_Y

    try:
        bg = _decode(bg_bytes)
        tm = _decode(tmpl_bytes)
        if bg is None or tm is None:
            return 120.0, DEFAULT_Y
        bg_gray = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY) if bg.ndim >= 3 else bg

        # 模板尺寸（用于筛轮廓）
        tw = th = 0
        tpl_y0 = tpl_y1 = 0
        if tm.ndim >= 3 and tm.shape[2] == 4:
            alpha = tm[:, :, 3]
            ys, xs = np.where(alpha > 0)
            if len(xs) == 0:
                return 120.0, DEFAULT_Y
            tpl_y0, tpl_y1 = int(ys.min()), int(ys.max()) + 1
            tw, th = int(xs.max()) - int(xs.min()) + 1, tpl_y1 - tpl_y0

        # ---- 方法 1：白色缺口轮廓 ----
        cand = _find_by_contour(bg_gray, tw, th, tpl_y0, tpl_y1)
        if cand is not None:
            return cand + X_OFFSET, DEFAULT_Y

        # ---- 方法 2：灰度 + mask 模板匹配 ----
        if tm.ndim >= 3 and tm.shape[2] == 4 and tw > 0:
            alpha = tm[:, :, 3]
            ys, xs = np.where(alpha > 0)
            y0, y1 = int(ys.min()), int(ys.max()) + 1
            x0, x1 = int(xs.min()), int(xs.max()) + 1
            rgb = tm[y0:y1, x0:x1, :3]
            mask = (alpha[y0:y1, x0:x1] > 0).astype(np.uint8) * 255
            templ = cv2.cvtColor(rgb, cv2.COLOR_BGR2GRAY)
            res = cv2.matchTemplate(bg_gray, templ, cv2.TM_CCORR_NORMED, mask=mask)
            _, score, _, loc = cv2.minMaxLoc(res)
            if score >= _MIN_SCORE:
                return float(loc[0]) + X_OFFSET, DEFAULT_Y

        # ---- 方法 3：Canny 兜底 ----
        return _edge_fallback(bg_gray, tm) + X_OFFSET, DEFAULT_Y
    except Exception:
        return 120.0, DEFAULT_Y


def _find_by_contour(bg_gray, tw: int, th: int, tpl_y0: int, tpl_y1: int) -> Optional[float]:
    """在背景图上找白色描边轮廓，挑与模板尺寸最接近的那个。

    缺口描边是闭合矩形/拼图形，宽高与 jigsaw 模板一致（约 42x43）。
    只接受宽高偏差在 ±25% 内、且与模板 y 区间有重叠的候选。
    """
    import cv2
    import numpy as np

    if tw <= 4 or th <= 4:
        return None

    best = None
    best_err = 1e9
    for thr in (200, 215, 230):
        _, bw = cv2.threshold(bg_gray, thr, 255, cv2.THRESH_BINARY)
        kernel = np.ones((3, 3), np.uint8)
        bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, kernel)
        cnts, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in cnts:
            x, y, w, h = cv2.boundingRect(c)
            area = cv2.contourArea(c)
            if area < tw * th * 0.25:
                continue
            # 尺寸偏差
            err = abs(w - tw) / tw + abs(h - th) / th
            if err > 0.5:
                continue
            # 与模板 y 区间至少重叠一半
            overlap = min(y + h, tpl_y1) - max(y, tpl_y0)
            if overlap < th * 0.4:
                continue
            if err < best_err:
                best_err, best = err, float(x)
    return best


def _edge_fallback(bg_gray, tm) -> float:
    """最末兜底：Canny 边缘匹配。"""
    try:
        import cv2
        import numpy as np
        if tm.ndim >= 3 and tm.shape[2] == 4:
            alpha = tm[:, :, 3]
            ys, xs = np.where(alpha > 0)
            y0, y1 = int(ys.min()), int(ys.max()) + 1
            x0, x1 = int(xs.min()), int(xs.max()) + 1
            roi = (alpha[y0:y1, x0:x1] > 0).astype(np.uint8) * 255
        else:
            y0, y1 = 0, tm.shape[0]
            roi = cv2.cvtColor(tm, cv2.COLOR_BGR2GRAY) if tm.ndim >= 3 else tm
        band = bg_gray[max(0, y0 - 2):min(bg_gray.shape[0], y1 + 2), :]
        e1 = cv2.Canny(band, 50, 150)
        e2 = cv2.Canny(roi, 50, 150)
        res = cv2.matchTemplate(e1, e2, cv2.TM_CCOEFF_NORMED)
        _, _, _, loc = cv2.minMaxLoc(res)
        return float(loc[0])
    except Exception:
        return 110.0
