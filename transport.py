"""网络传输底层模块。支持直连、ADB 转发与代理模式。"""
import json
import hashlib
import os
import random
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from typing import Dict, Any, Optional

import requests

from crypto import get_sign, APP_VERSION, BUILD_VERSION, APP_CODE
import device_identity

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_tls_session(impersonate: str):
    """尝试用 curl_cffi 建一个带真机 TLS 指纹的会话；不可用则返回 None。

    为什么需要：Python requests 走 OpenSSL 默认握手指纹，JA3/JA4 与真机的
    CFNetwork(iOS) / OkHttp(Android) 差异极大，服务端若做 TLS 指纹识别，
    光对齐请求头没用。curl_cffi 基于 curl-impersonate，能复现浏览器级指纹。
    """
    try:
        vendor = os.path.join(BASE_DIR, "vendor")
        if os.path.isdir(vendor) and vendor not in sys.path:
            sys.path.insert(0, vendor)
        from curl_cffi import requests as cffi_requests
        return cffi_requests.Session(impersonate=impersonate)
    except Exception:
        return None


def generate_device_id() -> str:
    """生成 16 位随机十六进制设备识别码。"""
    return secrets.token_hex(8)

class HttpTransport:
    DEFAULT_BASE_URL = "https://api.huachenjie.com/run-front"
    # 设备身份默认值来自真机机型池（device_identity），不再使用固定占位机型；
    # 实际取值由 device_identity.resolve_identity() 决定（显式 > 环境 > session.json 锁定值 > 新生成）。
    DEFAULT_MODEL = device_identity.FALLBACK_MODEL
    DEFAULT_SYS_VER = device_identity.FALLBACK_SYS_VER
    DEFAULT_PLATFORM = "2"
    DEFAULT_CHANNEL = "other"

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        token: Optional[str] = None,
        satoken: Optional[str] = None,
        device_id: Optional[str] = None,
        model_name: Optional[str] = None,
        system_version: Optional[str] = None,
        transport_mode: str = "direct",
        proxy: Optional[str] = None,
        adb_path: Optional[str] = None,
        adb_device: Optional[str] = None,
        timeout: int = 15
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token or ""
        self.satoken = satoken or ""
        # 设备身份：显式参数 > SHANDONG_DEVICE_ID/SHANDONG_MODEL/SHANDONG_SYSTEM_VERSION
        # > session.json 已锁定值 > 首次生成并落盘（设备不再每次运行随机变化）
        identity = device_identity.resolve_identity(
            device_id=device_id,
            model_name=model_name,
            system_version=system_version,
        )
        self.device_id = identity["deviceId"]
        self.model_name = identity["modelName"]
        self.system_version = identity["systemVersion"]
        # 客户端版本身份：默认 Android 标准版（8.7.7 / SD001 / platform=2）；
        # iOS 专业版为 2.0.6 / SDPRO001 / platform=1，可通过环境变量或
        # session.json 覆盖，UA 与 commonParams 会随之切换。
        self.app_version = os.environ.get("SHANDONG_APP_VERSION") or APP_VERSION
        self.build_version = os.environ.get("SHANDONG_BUILD_VERSION") or BUILD_VERSION
        self.app_code = os.environ.get("SHANDONG_APP_CODE") or APP_CODE
        self.platform = os.environ.get("SHANDONG_PLATFORM") or self.DEFAULT_PLATFORM
        self.screen_scale = os.environ.get("SHANDONG_SCREEN_SCALE") or "3.00"
        self.channel = os.environ.get("SHANDONG_CHANNEL") or self.DEFAULT_CHANNEL
        # cna：真机 Cookie 里的设备级标识（32 位 hex，阿里系惯用）。
        # 由 deviceId 派生，保证同一设备恒定、不同设备不同，无需额外持久化。
        self.cna = os.environ.get("SHANDONG_CNA") or hashlib.md5(
            f"cna:{self.device_id}".encode()).hexdigest()
        self.transport_mode = transport_mode
        self.proxy = proxy or os.environ.get("SHANDONG_PROXY")
        self.timeout = timeout
        self.adb_device = adb_device or os.environ.get("ADB_DEVICE")

        self.adb_path = adb_path or os.environ.get("ADB_PATH") or shutil.which("adb")
        if not self.adb_path:
            adb_candidates = [
                os.path.join(os.environ.get("ANDROID_SDK_ROOT", ""), "platform-tools", "adb.exe"),
                os.path.join(os.environ.get("ANDROID_HOME", ""), "platform-tools", "adb.exe"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk", "platform-tools", "adb.exe"),
                "/usr/bin/adb",
                "/usr/local/bin/adb",
            ]
            for adb_candidate in adb_candidates:
                if adb_candidate and os.path.exists(adb_candidate):
                    self.adb_path = adb_candidate
                    break
        self.session = requests.Session()
        # 默认直连：不自动采用系统/环境代理，避免被 WinINET 代理劫持后挂起。
        # 需要走系统代理时设置 SHANDONG_TRUST_ENV=1；显式 --proxy 始终生效。
        self.session.trust_env = os.environ.get("SHANDONG_TRUST_ENV", "").strip().lower() in ("1", "true", "yes")

        # --- TLS 指纹 -------------------------------------------------------
        # iOS 用 safari 指纹（CFNetwork 近似），Android 用 chrome 指纹
        # （curl_cffi 当前版本不支持 okhttp）。不可用时自动回退 requests。
        self.impersonate = "safari" if str(self.platform) == "1" else "chrome"
        self.tls_enabled = os.environ.get(
            "SHANDONG_TLS_IMPERSONATE", "1").strip().lower() not in ("0", "false", "no")
        self.tls_session = _load_tls_session(self.impersonate) if self.tls_enabled else None
        self.tls_backend = f"curl_cffi:{self.impersonate}" if self.tls_session is not None else "requests"

        # --- 请求节流 -------------------------------------------------------
        # 真机用户的操作节奏天然带间隔，脚本化的连续请求（毫秒级）是可被判定的
        # 机器人特征。这里给每个请求之间加随机停顿，让请求间隔分布贴近人手操作。
        # 关闭方式：SHANDONG_THROTTLE=0；自定义区间：SHANDONG_THROTTLE_RANGE=0.4,2.5
        self._last_request_at = 0.0
        self.throttle_enabled = os.environ.get("SHANDONG_THROTTLE", "1").strip().lower() not in ("0", "false", "no")
        self.throttle_range = self._parse_throttle_range(
            os.environ.get("SHANDONG_THROTTLE_RANGE", "")
        )

        if self.proxy and self.transport_mode == "proxy":
            self.session.proxies = {
                "http": self.proxy,
                "https": self.proxy,
            }

    def get_common_params(self) -> Dict[str, Any]:
        """组装客户端公共基础参数。

        键顺序与字段集合严格对齐真机（HAR 实测）：
            deviceId, buildVersion, appVersion, platform, appCode,
            systemVersion, modelName, timestamp
        注意两点：真机**没有** channel 字段；顺序也与上游不同。
        Python dict 保序，所以这里手写顺序即是最终 JSON 文本顺序 ——
        签名基于该文本，键序不一致会让每个请求的 sign 都与真机不同。
        """
        return {
            "deviceId": self.device_id,
            "buildVersion": self.build_version,
            "appVersion": self.app_version,
            "platform": int(self.platform),          # 真机是数字，不是字符串
            "appCode": self.app_code,
            "systemVersion": self.system_version,
            "modelName": self.model_name,
            "timestamp": str(int(time.time() * 1000)),
        }

    def _build_headers(self, raw_post_body: str, v: Optional[str] = None, api: Optional[str] = None,
                       webview: bool = False) -> Dict[str, str]:
        """构建通用请求头并计算签名。头集合按真机 HAR 逐项对齐。

        实测真机（iPhone 2.0.6）业务请求只有这些头：
            Accept: */*
            Accept-Encoding: gzip, deflate, br
            Accept-Language: zh-Hans-CN;q=1, de-CN;q=0.9
            Connection: keep-alive
            Content-Type: application/json
            Cookie: cna=<32位hex>
            e: 0
            User-Agent: ShanDong/<ver> (...)
            Authorization / satoken / sign
        上游代码额外发了 `timestamp`、`token`、`v`、`api` 四个头，真机一个都没有 ——
        这几个是最直白的脚本特征，已全部移除（参数保留以兼容旧调用，但不再写入头部）。

        `webview=True` 用于验证码接口：真机滑块是在 mh5.huachenjie.com 的 H5 页里做的，
        那条请求带 Origin/Referer/Sec-Fetch-*，缺了这些服务端可能直接判 result=false。
        """
        brand = self.model_name.split("|")[0] if "|" in self.model_name else self.model_name
        if str(self.platform) == "1":
            ua = f"ShanDong/{self.app_version} ({brand}; iOS {self.system_version}; Scale/{self.screen_scale})"
        else:
            ua = f"ShanDong/{self.app_version} ({brand};Android {self.system_version})"

        headers: Dict[str, str] = {
            "User-Agent": ua,
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "zh-Hans-CN;q=1, de-CN;q=0.9",
            "Connection": "keep-alive",
            "e": "0",
        }
        if self.cna:
            headers["Cookie"] = f"cna={self.cna}"
        if webview:
            headers["Origin"] = "https://mh5.huachenjie.com"
            headers["Referer"] = "https://mh5.huachenjie.com/"
            headers["Sec-Fetch-Dest"] = "empty"
            headers["Sec-Fetch-Mode"] = "cors"
            headers["Sec-Fetch-Site"] = "same-site"
        if self.token:
            headers["Authorization"] = self.token
        if self.satoken:
            headers["satoken"] = self.satoken
        if raw_post_body:
            headers["sign"] = get_sign(raw_post_body)
        return headers

    def post(self, endpoint: str, data: Optional[Dict[str, Any]] = None, add_common: bool = True,
             v: Optional[str] = None, api: Optional[str] = None,
             webview: bool = False) -> Dict[str, Any]:
        """发送 POST 请求。支持直连与 ADB 模式。

        webview=True 时补 Origin/Referer/Sec-Fetch-*（验证码等 H5 来源接口需要）。
        """
        url = endpoint if endpoint.startswith("http") else f"{self.base_url}{endpoint}"
        payload = {}
        if add_common:
            payload.update(self.get_common_params())
        if data:
            payload.update(data)

        raw_body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        headers = self._build_headers(raw_body, v=v, api=api, webview=webview)

        if self.transport_mode == "adb":
            return self._post_via_adb(url, raw_body, headers)
        return self._post_with_retry(url, raw_body, headers)

    def _proxy_map(self) -> Dict[str, str]:
        """curl_cffi 的代理映射：显式空串表示不走任何代理（含环境变量）。

        requests 用 trust_env=False 达到同样效果，curl_cffi 没有这个开关，
        必须显式传空串，否则会读到系统代理然后挂起。
        """
        if self.proxy and self.transport_mode == "proxy":
            return {"http": self.proxy, "https": self.proxy}
        return {"http": "", "https": ""}

    def _send(self, method: str, url: str, body: Optional[bytes] = None,
              headers: Optional[Dict[str, str]] = None,
              params: Optional[Dict[str, Any]] = None):
        """统一发送：优先真机 TLS 指纹（curl_cffi），失败回退 requests。"""
        if self.tls_session is not None:
            try:
                return self.tls_session.request(
                    method, url, data=body, headers=headers, params=params,
                    timeout=self.timeout, verify=False, proxies=self._proxy_map())
            except Exception:
                # TLS 后端抖动时回退一次，避免整条链路失败
                self.tls_session = None
                self.tls_backend = "requests(fallback)"
        if method.upper() == "GET":
            return self.session.get(url, params=params, headers=headers,
                                    timeout=self.timeout, verify=False)
        return self.session.post(url, data=body, headers=headers,
                                 timeout=self.timeout, verify=False)

    def _post_with_retry(self, url: str, raw_body: str, headers: Dict[str, str]) -> Dict[str, Any]:
        """直连 POST：节流 + 瞬时失败指数退避重试（最多 3 次）。"""
        attempt = 0
        result: Dict[str, Any] = {"code": -1, "message": "请求未执行"}
        while attempt < 3:
            self._throttle()
            attempt += 1
            try:
                resp = self._send("POST", url, body=raw_body.encode("utf-8"), headers=headers)
                result = resp.json()
            except Exception as e:
                result = {"code": -1, "message": f"网络传输异常: {e}"}
            if not self._is_retryable(result) or attempt >= 3:
                return result
            time.sleep(min(20.0, (2.0 ** attempt) + random.uniform(0.4, 2.0)))
        return result

    def get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """发送 GET 请求。"""
        url = endpoint if endpoint.startswith("http") else f"{self.base_url}{endpoint}"
        headers = self._build_headers("")
        if self.transport_mode == "adb":
            return self._get_via_adb(url, params, headers)
        attempt = 0
        result: Dict[str, Any] = {"code": -1, "message": "请求未执行"}
        while attempt < 3:
            self._throttle()
            attempt += 1
            try:
                resp = self._send("GET", url, headers=headers, params=params)
                result = resp.json()
            except Exception as e:
                result = {"code": -1, "message": f"网络传输异常: {e}"}
            if not self._is_retryable(result) or attempt >= 3:
                return result
            time.sleep(min(20.0, (2.0 ** attempt) + random.uniform(0.4, 2.0)))
        return result

    def _get_adb_target(self) -> Optional[str]:
        """获取目标 ADB 设备标识。"""
        if self.adb_device:
            return self.adb_device
        try:
            r = subprocess.run([self.adb_path, "devices"], capture_output=True, text=True, timeout=5)
            lines = [l for l in r.stdout.strip().splitlines() if "	device" in l]
            if lines:
                return lines[0].split("	")[0]
        except Exception:
            pass
        return None

    @staticmethod
    def _parse_throttle_range(raw: str):
        """解析 SHANDONG_THROTTLE_RANGE=最小,最大（秒）。非法时回退默认区间。"""
        default = (0.4, 2.5)
        if not raw:
            return default
        try:
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            if len(parts) != 2:
                return default
            lo, hi = float(parts[0]), float(parts[1])
            if lo < 0 or hi < lo:
                return default
            return (lo, hi)
        except Exception:
            return default

    def _throttle(self) -> None:
        """请求间隔抖动：距上次请求不足随机间隔时补足停顿。"""
        if not self.throttle_enabled:
            return
        lo, hi = self.throttle_range
        gap = random.uniform(lo, hi)
        now = time.monotonic()
        wait = gap - (now - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    @staticmethod
    def _is_retryable(res: Any) -> bool:
        """判断响应是否为可重试的瞬时失败（网络异常/限流/网关抖动）。"""
        if not isinstance(res, dict):
            return False
        code = res.get("code")
        if code == -1:
            return True
        msg = str(res.get("message") or "")
        for kw in ("网络传输异常", "超时", "timed out", "Timeout", "Connection",
                   "请稍后", "请求过于频繁", "频繁", "系统繁忙", "限流"):
            if kw in msg:
                return True
        return False

    def _post_via_adb(self, url: str, raw_body: str, headers: Dict[str, str]) -> Dict[str, Any]:
        """通过 ADB shell curl 发送 POST 请求。"""
        if not self.adb_path:
            return {"code": -2, "message": "未找到 ADB 执行文件路径"}
        dev = self._get_adb_target()
        dev_args = ["-s", dev] if dev else []

        remote_tmp = "/data/local/tmp/shandong_req.json"
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tf:
            tf.write(raw_body)
            local_tmp = tf.name

        try:
            push_cmd = [self.adb_path] + dev_args + ["push", local_tmp, remote_tmp]
            subprocess.run(push_cmd, capture_output=True, timeout=5, encoding="utf-8", errors="replace")

            header_args = []
            for hk, hv in headers.items():
                if not hv and hk != "k":
                    continue
                safe_v = str(hv).replace("'", "'\''")
                header_args.append(f"-H '{hk}: {safe_v}'")

            header_str = " ".join(header_args)
            curl_cmd = f"curl -s -k --compressed -X POST '{url}' {header_str} -d @{remote_tmp} --connect-timeout {self.timeout}"

            res = subprocess.run([self.adb_path] + dev_args + ["shell", curl_cmd], capture_output=True, encoding="utf-8", errors="replace", timeout=self.timeout + 10)
            out = res.stdout.strip()
            if not out:
                return {"code": -3, "message": f"ADB 请求无响应，错误: {res.stderr}"}
            try:
                return json.loads(out)
            except Exception:
                return {"code": -4, "message": "ADB 返回非 JSON", "raw": out}
        except Exception as e:
            return {"code": -1, "message": f"ADB 传输异常: {e}"}
        finally:
            if os.path.exists(local_tmp):
                try:
                    os.remove(local_tmp)
                except Exception:
                    pass
            subprocess.run([self.adb_path] + dev_args + ["shell", "rm", "-f", remote_tmp], capture_output=True)

    def _get_via_adb(self, url: str, params: Optional[Dict[str, Any]], headers: Dict[str, str]) -> Dict[str, Any]:
        """通过 ADB shell curl 发送 GET 请求。"""
        dev = self._get_adb_target()
        dev_args = ["-s", dev] if dev else []
        full_url = url
        if params:
            full_url += "?" + urllib.parse.urlencode(params)

        header_args = []
        for hk, hv in headers.items():
            if not hv: continue
            safe_v = str(hv).replace("'", "'\''")
            header_args.append(f"-H '{hk}: {safe_v}'")

        header_str = " ".join(header_args)
        curl_cmd = f"curl -s -k --compressed '{full_url}' {header_str} --connect-timeout {self.timeout}"
        res = subprocess.run([self.adb_path] + dev_args + ["shell", curl_cmd], capture_output=True, encoding="utf-8", errors="replace", timeout=self.timeout + 10)
        out = res.stdout.strip()
        if not out:
            return {"code": -3, "message": f"ADB GET 无响应: {res.stderr}"}
        try:
            return json.loads(out)
        except Exception:
            return {"code": -4, "message": "ADB 返回非 JSON", "raw": out}
