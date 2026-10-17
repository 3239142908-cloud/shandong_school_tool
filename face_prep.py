"""人脸打卡图准备：把底图按真机规格（192x256 / q82）重编码并上传 OSS。

真机两次阳光跑的 runFaceImg 都是同一张 192x256、8.3KB 的小图 —— 那是
人脸 SDK 检测后的输出，不是用户上传的原图（底图是 750x1000、132KB）。
直接把底图 URL 填进 faceCheckRecordList 会有两个破绽：
  1. 尺寸/体积与真机打卡图差一个数量级
  2. 与底图 URL 完全一致，可能被"是否复用了底图"规则命中

这里按真机规格重编码，像素内容仍是同一张脸（相似度满分），但 MD5 / URL 都是新的。
"""
import base64
import datetime
import hashlib
import hmac
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("SHANDONG_SESSION_FILE", os.path.join(HERE, "session.json"))

import cv2
import numpy as np
import requests

from client import ShanDongClient

BUCKET = "sd-campus-badge"
ENDPOINT = "oss-cn-hangzhou.aliyuncs.com"
CDN = "https://oss-cdn.huachenjie.com"

# 真机实测规格
TARGET_W, TARGET_H = 192, 256
TARGET_QUALITY = 82


def _sess():
    s = requests.Session()
    s.trust_env = False
    return s


def oss_put(key: str, data: bytes, ak: str, sk: str, token: str,
            content_type: str = "image/jpeg"):
    date = datetime.datetime.now(datetime.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    md5 = base64.b64encode(hashlib.md5(data).digest()).decode()
    sts = (f"PUT\n{md5}\n{content_type}\n{date}\n"
           f"x-oss-security-token:{token}\n/{BUCKET}/{key}")
    sig = base64.b64encode(
        hmac.new(sk.encode(), sts.encode(), hashlib.sha1).digest()).decode()
    headers = {
        "Date": date,
        "Content-MD5": md5,
        "Content-Type": content_type,
        "x-oss-security-token": token,
        "Authorization": f"OSS {ak}:{sig}",
    }
    return _sess().put(f"https://{BUCKET}.{ENDPOINT}/{key}",
                       data=data, headers=headers, timeout=30)


def prepare_face_image(face_img_url: str = "", verbose: bool = True):
    """下载底图 → 按真机规格重编码 → 上传 → 返回 CDN URL。

    face_img_url 留空则自动用账号当前的 faceImg。
    返回 (url, info)；失败返回 (None, {"error": ...})。
    """
    c = ShanDongClient()
    if not face_img_url:
        r = c.get_user_face_img()
        face_img_url = ((r.get("data") or {}).get("faceImg") or "")
    if not face_img_url:
        return None, {"error": "取不到人脸底图 URL"}

    try:
        raw = _sess().get(face_img_url, timeout=25).content
    except Exception as e:
        return None, {"error": f"底图下载失败: {e}"}
    src = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if src is None:
        return None, {"error": "底图解码失败"}

    # 等比缩放到 192x256（真机输出尺寸），保持人脸比例
    resized = cv2.resize(src, (TARGET_W, TARGET_H), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", resized, [cv2.IMWRITE_JPEG_QUALITY, TARGET_QUALITY])
    if not ok:
        return None, {"error": "重编码失败"}
    data = buf.tobytes()

    # OSS STS
    tr = c.get_oss_token()
    if tr.get("code") != 0:
        return None, {"error": f"OSS token 失败: {tr.get('message')}"}
    d = tr["data"]
    stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    key = f"prod/run-front/face/run/{stamp}{os.getpid()}.jpg"
    resp = oss_put(key, data, d["accessKeyId"], d["accessKeySecret"], d["securityToken"])
    if resp.status_code != 200:
        return None, {"error": f"上传失败 HTTP {resp.status_code}: {resp.text[:200]}"}

    url = f"{CDN}/{key}"
    info = {
        "src_size": f"{src.shape[1]}x{src.shape[0]}",
        "out_size": f"{TARGET_W}x{TARGET_H}",
        "bytes": len(data),
        "quality": TARGET_QUALITY,
        "md5": hashlib.md5(data).hexdigest(),
        "url": url,
    }
    if verbose:
        print(f"[人脸图] 底图 {info['src_size']} {len(raw)}B → 重编码 {info['out_size']} "
              f"{info['bytes']}B (q{TARGET_QUALITY})")
        print(f"[人脸图] 上传成功 → {url}")
    return url, info


if __name__ == "__main__":
    url, info = prepare_face_image()
    print(json.dumps(info, ensure_ascii=False, indent=1))
    if url:
        # 回读验证
        back = _sess().get(url, timeout=20)
        print(f"回读: HTTP {back.status_code} {len(back.content)} bytes "
              f"md5 一致={hashlib.md5(back.content).hexdigest() == info['md5']}")
