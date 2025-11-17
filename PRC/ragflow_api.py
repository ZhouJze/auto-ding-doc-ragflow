from __future__ import annotations

import json
import os
import time
from pathlib import Path
import logging
from typing import Iterable, List, Optional, Sequence, Tuple

import requests

_logger = logging.getLogger("ragflow_api")


"""
Ragflow API helpers

This module wraps common Ragflow operations used in the project:
  - upload PDF documents to a dataset
  - trigger parsing (ingestion) for a set of document IDs
  - delete documents from a dataset

Configuration is taken from environment variables by default:
  RAGFLOW_BASE          e.g. https://ragflow.example.com
  RAGFLOW_TOKEN         e.g. ragflow-xxxxxxxxxxxxxxxx
  RAGFLOW_DATASET_ID    dataset identifier string

All helpers expose optional parameters to override dataset/base/token at call time.
"""


def _env_base() -> str:
    base = os.getenv("RAGFLOW_BASE", "").rstrip("/")
    if not base:
        raise RuntimeError("RAGFLOW_BASE is not set")
    return base


def _env_token() -> str:
    token = os.getenv("RAGFLOW_TOKEN", "").strip()
    if not token:
        raise RuntimeError("RAGFLOW_TOKEN is not set")
    return token


def _env_dataset() -> str:
    ds = os.getenv("RAGFLOW_DATASET_ID", "").strip()
    if not ds:
        raise RuntimeError("RAGFLOW_DATASET_ID is not set")
    return ds


def _auth_headers(token: Optional[str] = None) -> dict:
    t = token or _env_token()
    return {
        "Authorization": f"Bearer {t}",
    }


def upload_document(
    file_path: Path | str,
    *,
    dataset_id: Optional[str] = None,
    base_url: Optional[str] = None,
    token: Optional[str] = None,
    timeout: float = 60.0,
) -> str:
    """Upload a single PDF to Ragflow. Returns created document ID.

    Args:
        file_path: path to a PDF file on disk
        dataset_id: override dataset id; defaults to RAGFLOW_DATASET_ID
        base_url: override base URL; defaults to RAGFLOW_BASE
        token: override API token; defaults to RAGFLOW_TOKEN
        timeout: request timeout in seconds
    """
    base = (base_url or _env_base()).rstrip("/")
    ds = dataset_id or _env_dataset()
    url = f"{base}/api/v1/datasets/{ds}/documents"

    p = Path(file_path)
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(str(p))

    files = {"file": (p.name, p.open("rb"), "application/pdf")}
    _logger.info(f"[ragflow] upload url={url} file={p.name} size={p.stat().st_size}")
    resp = requests.post(url, headers={**_auth_headers(token)}, files=files, timeout=timeout)
    _logger.info(f"[ragflow] upload status={resp.status_code}")
    resp.raise_for_status()
    data = resp.json()
    # Expected shape: { "code": 0, "data": [{"id": "..."}, ...] }
    if not isinstance(data, dict) or data.get("code") not in (0, "0"):
        _logger.error(f"[ragflow] upload failed body={data}")
        raise RuntimeError(f"Ragflow upload failed: {data}")
    items = data.get("data") or []
    if not items:
        _logger.error(f"[ragflow] upload empty data body={data}")
        raise RuntimeError(f"Ragflow upload returned empty data: {data}")
    doc_id = items[0].get("id")
    if not doc_id:
        _logger.error(f"[ragflow] upload missing id body={data}")
        raise RuntimeError(f"Ragflow upload missing document id: {data}")
    return str(doc_id)


def upload_documents(
    files: Sequence[Path | str],
    *,
    dataset_id: Optional[str] = None,
    base_url: Optional[str] = None,
    token: Optional[str] = None,
    timeout_per_file: float = 60.0,
) -> List[str]:
    """Upload multiple PDFs, returns list of created document IDs (order aligned with input)."""
    ids: List[str] = []
    for fp in files:
        doc_id = upload_document(fp, dataset_id=dataset_id, base_url=base_url, token=token, timeout=timeout_per_file)
        ids.append(doc_id)
    return ids


def parse_documents(
    doc_ids: Sequence[str],
    *,
    dataset_id: Optional[str] = None,
    base_url: Optional[str] = None,
    token: Optional[str] = None,
    timeout: float = 60.0,
    max_retries: int = 3,
    backoff: float = 1.0,
) -> bool:
    """Trigger parsing for the given document IDs.

    Returns True on success (HTTP 200 and code==0), False otherwise.
    
    Args:
        doc_ids: list of document IDs to parse
        dataset_id: override dataset id; defaults to RAGFLOW_DATASET_ID
        base_url: override base URL; defaults to RAGFLOW_BASE
        token: override API token; defaults to RAGFLOW_TOKEN
        timeout: request timeout in seconds (default 60.0)
        max_retries: maximum number of retry attempts (default 3)
        backoff: backoff multiplier for retry delays (default 1.0)
    """
    if not doc_ids:
        return True
    base = (base_url or _env_base()).rstrip("/")
    ds = dataset_id or _env_dataset()
    url = f"{base}/api/v1/datasets/{ds}/chunks"
    headers = {
        "Authorization": f"Bearer {token or _env_token()}",
        "Content-Type": "application/json",
    }
    body = {"document_ids": list(doc_ids)}
    
    for attempt in range(1, max_retries + 1):
        try:
            _logger.info(f"[ragflow] parse url={url} n={len(doc_ids)} attempt={attempt}")
            resp = requests.post(url, headers=headers, data=json.dumps(body), timeout=timeout)
            _logger.info(f"[ragflow] parse status={resp.status_code}")
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and data.get("code") == 0:
                    return True
            # 失败示例：{"code":102, "message":"`document_ids` is required"}
        except Exception as e:
            _logger.warning(f"[ragflow] parse exception attempt={attempt} err={e}")
        if attempt < max_retries:
            time.sleep(backoff * attempt)
    return False


def delete_documents(
    doc_ids: Sequence[str],
    *,
    dataset_id: Optional[str] = None,
    base_url: Optional[str] = None,
    token: Optional[str] = None,
    timeout: float = 30.0,
    max_retries: int = 3,
) -> bool:
    """Delete documents by ids. Returns True on success."""
    if not doc_ids:
        return True
    base = (base_url or _env_base()).rstrip("/")
    ds = dataset_id or _env_dataset()
    url = f"{base}/api/v1/datasets/{ds}/documents"
    headers = {
        "Authorization": f"Bearer {token or _env_token()}",
        "Content-Type": "application/json"
    }
    data = {"ids": list(doc_ids)}
    
    for attempt in range(1, max_retries + 1):
        try:
            _logger.info(f"[ragflow] delete url={url} n={len(doc_ids)} attempt={attempt}")
            resp = requests.delete(url, headers=headers, json=data, timeout=timeout)
            _logger.info(f"[ragflow] delete status={resp.status_code}")
            if resp.status_code == 200:
                result = resp.json()
                if isinstance(result, dict) and result.get("code") == 0:
                    return True
        except Exception as e:
            _logger.warning(f"[ragflow] delete exception attempt={attempt} err={e}")
        if attempt < max_retries:
            time.sleep(1.0 * attempt)
    return False


def update_document_metadata(
    document_id: str,
    meta_fields: dict,
    *,
    dataset_id: Optional[str] = None,
    base_url: Optional[str] = None,
    token: Optional[str] = None,
    timeout: float = 30.0,
    max_retries: int = 3,
) -> bool:
    """Update document metadata. Returns True on success.
    
    Args:
        document_id: the document ID to update
        meta_fields: dictionary of metadata fields to update, e.g. {"url": "https://..."}
        dataset_id: override dataset id; defaults to RAGFLOW_DATASET_ID
        base_url: override base URL; defaults to RAGFLOW_BASE
        token: override API token; defaults to RAGFLOW_TOKEN
        timeout: request timeout in seconds (default 30.0)
        max_retries: maximum number of retry attempts (default 3)
    """
    base = (base_url or _env_base()).rstrip("/")
    ds = dataset_id or _env_dataset()
    url = f"{base}/api/v1/datasets/{ds}/documents/{document_id}"
    headers = {
        "Authorization": f"Bearer {token or _env_token()}",
        "Content-Type": "application/json"
    }
    body = {"meta_fields": meta_fields}
    
    for attempt in range(1, max_retries + 1):
        try:
            _logger.info(f"[ragflow] update metadata url={url} doc_id={document_id} attempt={attempt}")
            resp = requests.put(url, headers=headers, json=body, timeout=timeout)
            _logger.info(f"[ragflow] update metadata status={resp.status_code}")
            if resp.status_code == 200:
                result = resp.json()
                if isinstance(result, dict) and result.get("code") == 0:
                    return True
        except Exception as e:
            _logger.warning(f"[ragflow] update metadata exception attempt={attempt} err={e}")
        if attempt < max_retries:
            time.sleep(1.0 * attempt)
    return False


