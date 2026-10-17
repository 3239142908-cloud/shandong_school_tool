"""本地会话持久化存储模块。负责保存与读取 Token 与设备指纹。

本模块是 session.json 的唯一读写入口：
- load_session()  读取
- save_session()  整体覆盖写入（原子替换，避免半截文件）
- update_session() 字段级合并写入（设备身份落盘走这里，保留 token 等字段）
- clear_session() 清除凭证；preserve 中的字段会被保留重新落盘（设备身份与账号解耦）
"""
import json
import os
from typing import Any, Dict, Optional

_DEFAULT_SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session.json")

# 兼容旧引用点；实际路径请用 session_file_path()（支持 SHANDONG_SESSION_FILE 覆盖）
SESSION_FILE = _DEFAULT_SESSION_FILE

# 模板占位值 / 无效值：读到即视为"未提供"
_PLACEHOLDER_EXACT = {
    "", "none", "null", "undefined", "nan",
    "genericdevice", "android|genericdevice",
}


def session_file_path() -> str:
    """返回当前会话文件路径。SHANDONG_SESSION_FILE 可显式覆盖（多账号 / 自检用）。"""
    override = os.environ.get("SHANDONG_SESSION_FILE", "").strip()
    return override or SESSION_FILE


def is_placeholder(value: Any) -> bool:
    """判断取值是否为模板占位值 / 无效值（YOUR_XXX、空串、GenericDevice 等）。"""
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    v = value.strip()
    if v.lower() in _PLACEHOLDER_EXACT:
        return True
    if v.upper().startswith("YOUR_"):
        return True
    return False


def _read_json(path: str) -> Dict[str, Any]:
    """读取 JSON 对象；容错 UTF-8 BOM（Windows 记事本另存会带 BOM）。"""
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def load_session() -> Dict[str, Any]:
    """读取本地会话配置。优先读取当前目录，回退读取 8.7.2 目录。"""
    path = session_file_path()
    if os.path.exists(path):
        try:
            return _read_json(path)
        except Exception:
            return {}
    if os.environ.get("SHANDONG_SESSION_FILE", "").strip():
        return {}
    fallback_872 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "8.7.2", "session.json")
    if os.path.exists(fallback_872):
        try:
            data = _read_json(fallback_872)
            if data:
                save_session(data)
                return data
        except Exception:
            pass
    return {}


def save_session(session_data: Dict[str, Any]) -> None:
    """保存当前会话到本地 session.json（临时文件 + 原子替换）。"""
    path = session_file_path()
    tmp = f"{path}.tmp"
    try:
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(session_data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def update_session(patch: Dict[str, Any]) -> Dict[str, Any]:
    """字段级合并写入：读取现有会话 → 覆盖 patch 中的字段 → 落盘。返回合并结果。"""
    data = load_session() or {}
    data.update(patch or {})
    save_session(data)
    return data


def clear_session(preserve: Optional[Dict[str, Any]] = None) -> None:
    """清除本地保存的凭证。preserve 中的字段（如设备身份）会保留并重新落盘。"""
    if preserve:
        save_session(dict(preserve))
        return
    path = session_file_path()
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass
