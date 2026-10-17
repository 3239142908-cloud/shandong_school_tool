"""用 STS 临时凭证手工做 OSS V1 签名上传（oss2 装不上，自己算签名）。"""
import base64
import datetime
import hashlib
import hmac
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ["SHANDONG_SESSION_FILE"] = os.path.join(HERE, "session.json")

import requests

from client import ShanDongClient

BUCKET = "sd-campus-badge"
ENDPOINT = "oss-cn-hangzhou.aliyuncs.com"


def oss_put(key: str, data: bytes, ak: str, sk: str, token: str,
            content_type: str = "image/jpeg"):
    date = datetime.datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")
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
    url = f"https://{BUCKET}.{ENDPOINT}/{key}"
    s = requests.Session()
    s.trust_env = False
    return s.put(url, data=data, headers=headers, timeout=30), url


def main():
    c = ShanDongClient()
    r = c.get_oss_token()
    if r.get("code") != 0:
        print("取 OSS token 失败:", r)
        return 1
    d = r["data"]
    ak, sk, tok = d["accessKeyId"], d["accessKeySecret"], d["securityToken"]
    print(f"STS: {ak}  目录={d.get('directory')}  过期={d.get('expiration')}")

    img_path = os.path.join(HERE, "_face_base.jpg")
    if not os.path.exists(img_path):
        print("缺 _face_base.jpg")
        return 1
    data = open(img_path, "rb").read()
    print(f"底图: {len(data)} bytes  md5={hashlib.md5(data).hexdigest()}")

    # 目标路径：与真机人脸打卡图同目录 prod/run-front/face/run/
    stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    key = f"prod/run-front/face/run/{stamp}{os.getpid()}.jpg"
    print(f"上传目标: {key}")

    resp, url = oss_put(key, data, ak, sk, tok)
    print(f"HTTP {resp.status_code}")
    if resp.status_code == 200:
        print("上传成功:", url)
        # 验证可访问
        s = requests.Session()
        s.trust_env = False
        v = s.get(url, timeout=20)
        print(f"回读: HTTP {v.status_code}  {len(v.content)} bytes  "
              f"md5 一致={hashlib.md5(v.content).hexdigest() == hashlib.md5(data).hexdigest()}")
        print()
        print("可用于 --face-img 的新 URL：")
        print(url)
    else:
        print("响应:", resp.text[:400])
    return 0


if __name__ == "__main__":
    sys.exit(main())
