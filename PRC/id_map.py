from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional, Tuple

# 线程安全的 ID 映射缓存与持久化封装
# 设计目标：
# - 读多写少的场景，内存缓存 + 显式持久化
# - 可在多个模块中安全复用（模块级单例缓存）
# - 持久化文件默认存放在 /data 目录（与项目其余数据一致）


_DEFAULT_PATH = Path("/data/id_mapping.json")
_lock = threading.RLock()
_cache: Dict[str, Any] = {}
_loaded: bool = False
_path: Path = _DEFAULT_PATH


def _ensure_parent(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)


def set_path(p: Path | str) -> None:
    """设置映射文件路径（需在 load/save 之前调用）。"""
    global _path
    with _lock:
        _path = Path(p)


def get_path() :
    """返回当前使用的映射文件路径。"""
    with _lock:
        return _path


def load(reset: bool = False) -> Dict[str, Any]:
    """从磁盘加载映射到内存缓存。

    Args:
        reset: 为 True 时强制丢弃当前内存缓存并从磁盘重载。

    Returns:
        内存中的映射字典（可读不可变引用，修改请使用 put/delete/update）。
    """
    global _loaded, _cache
    with _lock:
        if not _loaded or reset:
            _cache = {}
            if _path.exists() and _path.is_file():
                try:
                    data = json.loads(_path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        _cache.update(data)
                except Exception:
                    # 读取失败时保持空缓存，调用方可选择忽略或提示
                    _cache = {}
            _loaded = True
        return _cache


def save() -> None:
    """将内存缓存持久化到磁盘。"""
    with _lock:
        _ensure_parent(_path)
        tmp = _path.with_suffix(".tmp")
        tmp.write_text(json.dumps(_cache, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_path)


def clear(persist: bool = False) -> None:
    """清空内存缓存；如 persist=True，同步清空磁盘文件。"""
    global _cache, _loaded
    with _lock:
        _cache = {}
        _loaded = True
        if persist:
            _ensure_parent(_path)
            _path.write_text("{}", encoding="utf-8")


def size() -> int:
    with _lock:
        return len(_cache)


def get(key: str, default: Any = None) -> Any:
    with _lock:
        return _cache.get(key, default)


def put(key: str, value: Any, auto_save: bool = False) -> None:
    """写入/覆盖单个映射。

    Args:
        key: 业务 ID（例如 dentryUuid/docId/自定义键）
        value: 映射的值（可为文件路径、元信息 dict 等）
        auto_save: 是否在写入后立即持久化到磁盘
    """
    with _lock:
        _cache[str(key)] = value
        if auto_save:
            save()


def put_many(pairs: Iterable[Tuple[str, Any]], auto_save: bool = False) -> None:
    with _lock:
        for k, v in pairs:
            _cache[str(k)] = v
        if auto_save:
            save()


def delete(key: str, auto_save: bool = False) -> Optional[Any]:
    with _lock:
        val = _cache.pop(str(key), None)
        if auto_save:
            save()
        return val


def items() -> Iterator[Tuple[str, Any]]:
    with _lock:
        # 返回一个浅拷贝的 items 迭代器，避免并发修改问题
        return iter(list(_cache.items()))


def ensure_initialized(path: Optional[Path | str] = None) -> None:
    """确保缓存已加载，必要时可指定自定义路径。"""
    if path is not None:
        set_path(path)
    load()


def get_ragflow_doc_id(key: str, default: Any = None) -> Any:
    """获取指定 key 对应的 ragflow_doc_id。
    
    Args:
        key: 映射的键（uuid）
        default: 如果未找到或格式不正确时返回的默认值
    
    Returns:
        ragflow_doc_id，如果 value 是字典则提取 ragflow_doc_id，否则返回 None
    """
    with _lock:
        value = _cache.get(key, default)
        if value is None or value == default:
            return default
        # 新格式：字典
        if isinstance(value, dict):
            return value.get('ragflow_doc_id', default)
        # 如果不是字典格式，返回 None（新版本只支持字典格式）
        return default


def put_ragflow_mapping(
    uuid: str,
    ragflow_doc_id: str,
    auto_save: bool = False
) -> None:
    """写入 ragflow 映射关系（新格式）。
    
    Args:
        uuid: 钉钉文档的 UUID
        ragflow_doc_id: Ragflow 文档 ID
        auto_save: 是否立即持久化
    """
    ding_doc_url = f"https://alidocs.dingtalk.com/i/nodes/{uuid}"
    value = {
        "ragflow_doc_id": ragflow_doc_id,
        "ding_doc_url": ding_doc_url
    }
    put(uuid, value, auto_save=auto_save)


# 默认在模块导入时尝试按默认路径惰性加载（仅在首次访问 load/get/put 时真正读取）

