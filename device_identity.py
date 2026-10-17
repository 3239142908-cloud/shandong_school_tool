"""设备身份加固模块：真机机型池 + systemVersion 合法绑定 + deviceId 落盘锁定。

职责
----
1. 真机机型池：modelName 格式 "品牌|机型"（与 transport._build_headers 的 UA 取值一致），
   每项绑定该机型在 Android 11/12/13/14 范围内真实出现过的 systemVersion 取值集合
   （首发版本 → 末次 OTA 版本，闭区间）。池内机型与版本禁止混搭。
2. deviceId 语义修正：16 位小写 hex（与 transport.generate_device_id 同格式），
   首次生成后写入 session.json 永久锁定；已有 session 时不再重新随机。
3. 身份优先级：显式参数 > 环境变量 > session.json 已锁定值 > 新生成。
   环境变量：SHANDONG_DEVICE_ID / SHANDONG_MODEL / SHANDONG_SYSTEM_VERSION。
4. 首次运行自动生成并落盘；此后 get_identity() / resolve_identity() 恒返回同一组值。

不改动：crypto.py 任何常量、transport 的 base_url / UA 格式 / 签名调用 / 接口路径与参数名。

调试通道
--------
SHANDONG_SESSION_FILE     指向替代 session.json（多账号 / 自检用，避免污染正式会话）
SHANDONG_ALLOW_PLACEHOLDER_MODEL=1  允许显式指定 Android|GenericDevice（默认拒绝该占位值）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

import session_store

# 服务端 systemVersion 取值域（字符串，与现有代码一致，例如 "12"）
ANDROID_VERSION_POOL: Tuple[str, ...] = ("11", "12", "13", "14")

DEVICE_ID_HEX_LEN = 16
# deviceId 两种真实形态：
#   Android 标准版  16 位小写 hex（secrets.token_hex(8)）
#   iOS 专业版      40 位小写 hex（实测 <IOS_DEVICE_ID_40HEX>）
_DEVICE_ID_RE = re.compile(r"^[0-9a-f]{16,64}$")
_MODEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*\|[A-Za-z0-9][A-Za-z0-9._\- ]*$")

# 历史死值（本工具旧版的统一占位机型），出现即视为无效身份
LEGACY_PLACEHOLDER_MODELS = ("Android|GenericDevice", "android|genericdevice")

# (modelName, 首发 systemVersion, 末次 OTA systemVersion, 备注)
_RAW_PROFILES: Sequence[Tuple[str, str, str, str]] = (
    # --- Xiaomi / Redmi ---
    ("Xiaomi|M2102J2SC", "11", "13", "Redmi K40 中国版，MIUI 12 → MIUI 14"),
    ("Xiaomi|M2012K11AC", "11", "13", "Redmi K40 同代备案机型"),
    ("Xiaomi|2107119DC", "11", "13", "小米 11"),
    ("Xiaomi|2201123C", "12", "14", "小米 12，A12 首发 → HyperOS A14"),
    ("Xiaomi|2206123SC", "12", "14", "小米 12S"),
    ("Xiaomi|22081212C", "12", "14", "Redmi K50 至尊版"),
    ("Xiaomi|2211133C", "13", "14", "小米 13，A13 首发 → HyperOS A14"),
    ("Xiaomi|23127PN0CC", "14", "14", "小米 14，A14 首发"),
    ("Redmi|22041216C", "12", "14", "Redmi Note 11T Pro，Build.BRAND=Redmi"),
    # --- HUAWEI（HarmonyOS 3/4 对第三方上报 Android API 12）---
    ("HUAWEI|ELS-AN00", "12", "12", "P40 Pro，HarmonyOS 3/4 上报 Android 12"),
    ("HUAWEI|ANA-AN00", "12", "12", "P40，HarmonyOS 3/4 上报 Android 12"),
    ("HUAWEI|NOH-AN00", "12", "12", "Mate 40 Pro，HarmonyOS 3/4 上报 Android 12"),
    # --- HONOR ---
    ("HONOR|ANY-AN00", "11", "13", "荣耀 X30，A11 首发 → MagicOS 7 A13"),
    ("HONOR|FNE-NX9", "12", "14", "荣耀 70，A12 首发"),
    ("HONOR|ELP-AN00", "12", "14", "荣耀 Magic4"),
    # --- OPPO ---
    ("OPPO|PCLM10", "11", "11", "Reno Ace，ColorOS 11 / Android 11 为末版"),
    ("OPPO|PEDM00", "11", "13", "Find X3"),
    ("OPPO|PGFM10", "13", "14", "Find X6"),
    # --- vivo ---
    ("vivo|V2055A", "11", "13", "iQOO Neo5"),
    ("vivo|V2049A", "11", "13", "vivo X60"),
    ("vivo|V2141A", "11", "13", "iQOO 8"),
    # --- realme ---
    ("realme|RMX3366", "11", "13", "realme GT 大师探索版"),
    ("realme|RMX3301", "12", "14", "realme GT2 Pro"),
    ("realme|RMX3706", "13", "14", "realme GT Neo5 SE"),
    # --- OnePlus ---
    ("OnePlus|LE2100", "11", "13", "一加 9"),
    ("OnePlus|NE2210", "12", "14", "一加 10 Pro"),
    ("OnePlus|MT2110", "11", "13", "一加 9RT"),
    ("OnePlus|PJD110", "14", "14", "一加 12，A14 首发"),
    # --- Samsung（Galaxy A55 5G 国行）---
    ("Samsung|SM-A5560", "12", "14", "Galaxy A55 5G，A12 出厂"),
)


def _expand_range(version_from: str, version_to: str) -> Tuple[str, ...]:
    """展开 [首发, 末次 OTA] 闭区间为合法版本集合。"""
    lo = ANDROID_VERSION_POOL.index(version_from)
    hi = ANDROID_VERSION_POOL.index(version_to)
    if lo > hi:
        lo, hi = hi, lo
    return ANDROID_VERSION_POOL[lo:hi + 1]


MODEL_PROFILES: Dict[str, Dict[str, Any]] = {}
for _model, _v_from, _v_to, _note in _RAW_PROFILES:
    if _model in MODEL_PROFILES:
        raise ValueError(f"机型池存在重复条目: {_model}")
    _versions = _expand_range(_v_from, _v_to)
    if not _versions:
        raise ValueError(f"机型 {_model} 版本区间为空")
    MODEL_PROFILES[_model] = {
        "modelName": _model,
        "brand": _model.split("|", 1)[0],
        "versions": _versions,
        "note": _note,
    }

MODEL_POOL: Tuple[str, ...] = tuple(MODEL_PROFILES.keys())

# 机型池兜底项（仅用于元数据，正常路径由 resolve_identity 决定）
FALLBACK_MODEL = "Xiaomi|M2102J2SC"
FALLBACK_SYS_VER = MODEL_PROFILES[FALLBACK_MODEL]["versions"][1]  # "12"


# --------------------------------------------------------------------------
# 基础取值 / 校验
# --------------------------------------------------------------------------
def generate_device_id() -> str:
    """生成 16 位小写十六进制 deviceId（与 transport.generate_device_id 格式完全一致）。"""
    return secrets.token_hex(DEVICE_ID_HEX_LEN // 2)


# 语义别名：device_identity 内部使用
new_device_id = generate_device_id


def _clean(value: Any) -> Optional[str]:
    """清洗输入：剥离空白、丢弃占位值，返回 None 表示"未提供"。"""
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if not value:
        return None
    if session_store.is_placeholder(value):
        return None
    if value in LEGACY_PLACEHOLDER_MODELS:
        if os.environ.get("SHANDONG_ALLOW_PLACEHOLDER_MODEL", "").strip() in ("1", "true", "yes"):
            return value
        print(
            f"[device_identity] 已忽略占位机型 {value!r}（该值在真机库中不存在，会构成作弊聚类）；"
            "如确需使用请设置 SHANDONG_ALLOW_PLACEHOLDER_MODEL=1",
            file=sys.stderr,
        )
        return None
    return value


def norm_device_id(value: Any) -> Optional[str]:
    """规范化 deviceId：合法返回 16 位小写 hex，否则返回 None。"""
    cleaned = _clean(value)
    if not cleaned:
        return None
    cleaned = cleaned.lower()
    return cleaned if _DEVICE_ID_RE.match(cleaned) else None


def is_known_model(model_name: Any) -> bool:
    return isinstance(model_name, str) and model_name.strip() in MODEL_PROFILES


def versions_for(model_name: str) -> Tuple[str, ...]:
    """返回该机型的合法 systemVersion 集合；未知机型返回空元组。"""
    profile = MODEL_PROFILES.get(str(model_name).strip())
    return tuple(profile["versions"]) if profile else ()


def is_valid_pair(model_name: Any, system_version: Any) -> bool:
    """机型与 systemVersion 是否匹配（池内机型必须命中其合法集合）。"""
    if not is_known_model(model_name) or not isinstance(system_version, str):
        return False
    return system_version.strip() in versions_for(model_name)


def nearest_version(model_name: str, requested: Optional[str]) -> str:
    """在机型合法集合内取与 requested 最接近的版本（池外机型回退兜底版本）。"""
    versions = versions_for(model_name)
    if not versions:
        return FALLBACK_SYS_VER
    if requested and requested in versions:
        return requested
    if requested and requested.isdigit():
        return min(versions, key=lambda v: (abs(int(v) - int(requested)), int(v)))
    return versions[-1]


def _pick_version(model_name: str) -> str:
    versions = versions_for(model_name) or (FALLBACK_SYS_VER,)
    return secrets.choice(list(versions))


def build_new_identity() -> Dict[str, str]:
    """生成一组全新的、机型与版本匹配的真机身份（不落盘）。"""
    model = secrets.choice(list(MODEL_POOL))
    return {
        "deviceId": generate_device_id(),
        "modelName": model,
        "systemVersion": _pick_version(model),
    }


# --------------------------------------------------------------------------
# session.json 读写（唯一入口：session_store）
# --------------------------------------------------------------------------
def load_stored_fields() -> Dict[str, str]:
    """读取 session.json 中的设备身份字段（逐字段清洗，不做合法性修复）。"""
    data = session_store.load_session() or {}
    fields: Dict[str, str] = {}
    device = norm_device_id(data.get("deviceId"))
    if device:
        fields["deviceId"] = device
    model = _clean(data.get("modelName"))
    if model:
        fields["modelName"] = model
    version = _clean(data.get("systemVersion"))
    if version:
        fields["systemVersion"] = version
    return fields


def load_locked_identity() -> Optional[Dict[str, str]]:
    """只读读取"已锁定身份"：三元组齐备且机型/版本匹配才返回，否则返回 None。"""
    fields = load_stored_fields()
    device = fields.get("deviceId")
    model = fields.get("modelName")
    version = fields.get("systemVersion")
    if not (device and model and version):
        return None
    if not is_valid_pair(model, version):
        return None
    return {"deviceId": device, "modelName": model, "systemVersion": version}


def _persist_identity(identity: Dict[str, str]) -> None:
    """把身份写回 session.json（合并写入，保留 token 等其它字段）。"""
    session_store.update_session({
        "deviceId": identity["deviceId"],
        "modelName": identity["modelName"],
        "systemVersion": identity["systemVersion"],
    })


# --------------------------------------------------------------------------
# 身份解析（核心）
# --------------------------------------------------------------------------
def resolve_identity(
    device_id: Optional[str] = None,
    model_name: Optional[str] = None,
    system_version: Optional[str] = None,
    persist: bool = True,
) -> Dict[str, str]:
    """解析出本次运行使用的设备身份，必要时落盘锁定。

    优先级：显式参数 > 环境变量 > session.json 已锁定值 > 新生成。
    - 池内机型：systemVersion 一定落在该机型合法集合内（不合法则就近修正）。
    - 池外机型（调用方显式指定）：保留原值，配对风险由调用方承担。
    - deviceId：已锁定且合法则永不变动；无合法值才生成新的并落盘。
    """
    stored = load_stored_fields()

    # --- 1. 机型 ---
    model: Optional[str] = None
    model_source = "generated"
    for source, candidate in (
        ("explicit", _clean(model_name)),
        ("env", _clean(os.environ.get("SHANDONG_MODEL"))),
        ("session", stored.get("modelName")),
    ):
        if candidate:
            model, model_source = candidate, source
            break
    if not model:
        model, model_source = secrets.choice(list(MODEL_POOL)), "generated"

    # --- 2. systemVersion ---
    explicit_ver = _clean(system_version)
    env_ver = _clean(os.environ.get("SHANDONG_SYSTEM_VERSION"))
    if explicit_ver:
        version, version_source = explicit_ver, "explicit"
    elif env_ver:
        version, version_source = env_ver, "env"
    elif stored.get("systemVersion"):
        version, version_source = stored["systemVersion"], "session"
    else:
        version, version_source = _pick_version(model), "generated"

    # 池内机型强制版本合法（禁止机型 / 版本混搭）
    if is_known_model(model) and version not in versions_for(model):
        fixed = nearest_version(model, version)
        print(
            f"[device_identity] 机型 {model} 不支持 systemVersion={version!r}（{version_source} 来源），"
            f"已就近修正为 {fixed!r}",
            file=sys.stderr,
        )
        version = fixed

    # 池外机型且版本同样是占位值 → 整体走新身份，避免留下无效指纹
    if not is_known_model(model) and version_source == "generated":
        model = secrets.choice(list(MODEL_POOL))
        version = _pick_version(model)

    # --- 3. deviceId ---
    candidate = None
    for source, value in (
        ("explicit", device_id),
        ("env", os.environ.get("SHANDONG_DEVICE_ID")),
        ("session", stored.get("deviceId")),
    ):
        norm = norm_device_id(value)
        if norm:
            candidate = norm
            break
    device = candidate or generate_device_id()

    identity = {"deviceId": device, "modelName": model, "systemVersion": version}

    if persist:
        already_locked = (
            stored.get("deviceId") == identity["deviceId"]
            and stored.get("modelName") == identity["modelName"]
            and stored.get("systemVersion") == identity["systemVersion"]
        )
        if not already_locked:
            _persist_identity(identity)

    return identity


def get_identity(persist: bool = True) -> Dict[str, str]:
    """锁定读取函数：返回本机设备身份；首次调用生成并落盘，之后恒返回同一组值。"""
    return resolve_identity(persist=persist)


def rotate_identity() -> Dict[str, str]:
    """显式更换设备身份（等价于换新手机），生成新三元组并落盘。"""
    identity = build_new_identity()
    _persist_identity(identity)
    return identity


# --------------------------------------------------------------------------
# 自检 / CLI
# --------------------------------------------------------------------------
def selftest(samples: int = 200) -> int:
    """机型池与版本匹配自检；返回进程退出码（0 = 通过）。"""
    failures: List[str] = []
    print(f"[selftest] 机型池条目数 = {len(MODEL_POOL)}")

    if len(MODEL_POOL) < 20:
        failures.append(f"机型池不足 20 组：{len(MODEL_POOL)}")

    for model in MODEL_POOL:
        versions = versions_for(model)
        if not versions:
            failures.append(f"{model} 无合法 systemVersion")
        for v in versions:
            if v not in ANDROID_VERSION_POOL:
                failures.append(f"{model} 版本 {v} 不在 {ANDROID_VERSION_POOL}")
        if not _MODEL_RE.match(model):
            failures.append(f"{model} 不符合 品牌|机型 格式")

    distribution: Dict[str, int] = {}
    version_distribution: Dict[str, int] = {}
    mismatches = 0
    bad_ids = 0
    for _ in range(samples):
        ident = build_new_identity()
        model, version, device = ident["modelName"], ident["systemVersion"], ident["deviceId"]
        distribution[model] = distribution.get(model, 0) + 1
        version_distribution[version] = version_distribution.get(version, 0) + 1
        if not is_valid_pair(model, version):
            mismatches += 1
            print(f"  [!] 机型/版本不匹配: {model} / {version}")
        if not _DEVICE_ID_RE.match(device):
            bad_ids += 1
            print(f"  [!] deviceId 格式非法: {device!r}")

    # 格式一致性：与 transport.generate_device_id 同格式
    transport_style = re.compile(r"^[0-9a-f]{16}$")
    if not transport_style.match(secrets.token_hex(8)):
        failures.append("deviceId 生成格式与 transport.generate_device_id 不一致")

    print(f"[selftest] 抽样次数 = {samples}")
    print("[selftest] 机型分布：")
    for model in sorted(distribution):
        print(f"    {model:<22} x{distribution[model]:<4} versions={','.join(versions_for(model))}")
    print("[selftest] 版本分布：" + ", ".join(f"{v}:{c}" for v, c in sorted(version_distribution.items())))
    print(f"[selftest] 机型/版本不匹配 = {mismatches}")
    print(f"[selftest] deviceId 格式非法 = {bad_ids}")

    if mismatches or bad_ids:
        failures.append(f"抽样校验失败：不匹配 {mismatches} 处，格式非法 {bad_ids} 处")

    if failures:
        for f in failures:
            print(f"[selftest] FAIL: {f}")
        print("[selftest] 结果 = 失败")
        return 1
    print(f"[selftest] 结果 = 通过（0 处不匹配 / {len(MODEL_POOL)} 组机型）")
    return 0


def _main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="闪动校园设备身份管理")
    sub = parser.add_subparsers(dest="cmd")
    p_self = sub.add_parser("selftest", help="机型池与版本匹配自检")
    p_self.add_argument("--samples", type=int, default=200)
    sub.add_parser("show", help="显示当前锁定身份")
    sub.add_parser("pool", help="列出机型池")
    sub.add_parser("rotate", help="更换设备身份并落盘")
    args = parser.parse_args(argv)

    cmd = args.cmd or "show"
    if cmd == "selftest":
        return selftest(args.samples)
    if cmd == "pool":
        print(json.dumps(
            {m: list(MODEL_PROFILES[m]["versions"]) for m in MODEL_POOL},
            ensure_ascii=False, indent=2,
        ))
        return 0
    if cmd == "rotate":
        print(json.dumps(rotate_identity(), ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(get_identity(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
