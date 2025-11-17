import json
import re
import time
from pathlib import Path
from typing import Dict, Any


ILLEGAL = r'[<>:"/\\|?*]'
ILLEGAL_RE = re.compile(ILLEGAL)


def sanitize_name(name: str, max_len: int = 100) -> str:
    if not name:
        return "untitled"
    n = ILLEGAL_RE.sub("_", name).strip()
    if not n:
        n = "untitled"
    return n[:max_len]


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def file_exists_nonempty(p: Path) -> bool:
    return p.exists() and p.is_file() and p.stat().st_size > 0


def load_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        with path.open('r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: Path, data: Any) -> None:
    ensure_dir(path.parent)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def backoff_delays(base: float = 1.0, factor: float = 3.0, attempts: int = 3):
    d = base
    for _ in range(attempts):
        yield d
        d *= factor


def sleep(s: float) -> None:
    time.sleep(s)


