import argparse
import sys
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
import os
import logging
import time
from datetime import datetime, timedelta
import hmac
import base64
import hashlib
import urllib.parse
import requests

from playwright.sync_api import sync_playwright, BrowserContext, Page
from . import id_map
from . import ragflow_api

try:
    # 作为包运行：python -m auto_downloader.PRC.main
    from .utils import ensure_dir, sanitize_name, file_exists_nonempty, load_json, save_json, backoff_delays, sleep
except Exception:
    # 直接脚本运行：python auto_downloader/PRC/main.py
    import pathlib as _pathlib
    import sys as _sys
    _sys.path.append(str(_pathlib.Path(__file__).resolve().parent))
    from utils import ensure_dir, sanitize_name, file_exists_nonempty, load_json, save_json, backoff_delays, sleep


HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
# 数据目录统一放在 /data 下
TMP_DIR = Path('/data/download')
STATE_PATH = Path('/data/export_state.json')
INJECT_FILE = HERE / 'tiny_alidocs_api.js'
# 使用环境变量 USER_DATA_DIR，与 login_only.py 保持一致
PERSIST_DIR = Path(os.getenv("USER_DATA_DIR", "/app/persistent_context/Default"))
LOG_DIR = Path('/data/log')


def run_full_update() -> None:
    """全量更新：从 2000-01-01 开始（极早时间），等价于全量。"""
    # 2000-01-01 00:00:00 的 Unix 时间戳（秒）
    ts_2000_01_01 = 946684800
    log("执行全量更新 ...")
    run_update(ts_2000_01_01)


def run_incremental_update() -> None:
    """增量更新：取"昨天凌晨"（昨日 00:00:00）的 Unix 秒并执行。"""
    _setup_incremental_logging()
    yesterday_midnight = (datetime.now() - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    ts = int(yesterday_midnight.timestamp())
    log(f"执行增量更新，起始时间: {yesterday_midnight.isoformat()} (ts={ts}) ...")
    run_update(ts)


def _build_targets_from_env_or_args(args_urls: Optional[List[str]] = None) -> List[str]:
    targets: List[str] = []
    if args_urls:
        targets.extend([u for u in args_urls if str(u).strip()])
    else:
        env_urls = os.getenv('TARGET_URLS', '') or ''
        if env_urls.strip():
            raw = [x for part in env_urls.split('\n') for x in part.split(',')]
            raw2 = [x for part in raw for x in part.split(';')]
            targets.extend([x.strip() for x in raw2 if x.strip()])
        else:
            single = os.getenv('TARGET_URL', '').strip()
            if single:
                targets.append(single)
    return targets


def run_update(update_time: int) -> None:
    """单线程更新流程。

    步骤：
    1) 解析目标列表（TARGET_URLS/TARGET_URL 或已存在的命令行 urls）
    2) 列表页采集 → 过滤出需要导出的 items（仅 adoc/axls，且 updatedTime/1000 >= update_time）
    3) 收集并删除旧文档（根据 uuid→doc_id 映射）
    4) 逐个导出下载 PDF → 上传 RagFlow → 更新映射 → 清理 PDF；每 10 个触发解析
    5) 同步删除：本地映射中存在但这次未出现的 uuid 批量删除 RagFlow 文档并删除映射
    6) 保存映射并打印统计
    """
    # 0. 基本配置
    headless = os.getenv('HEADLESS', 'true').lower() == 'true'
    targets = _build_targets_from_env_or_args()
    if not targets:
        log('run_update: 未提供任何目标（TARGET_URLS/TARGET_URL 为空），退出。')
        return

    # 1. 初始化映射
    id_map.ensure_initialized()
    before_keys = set(k for k, _ in list(id_map.items()))

    # 2. 浏览器与列表采集
    all_selected_items: List[Dict[str, Any]] = []
    all_seen_uuid_set = set()  # 本次遍历到的全部文件 uuid（非仅可导出）
    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(
            user_data_dir=str(PERSIST_DIR),
            headless=headless,
            args=['--disable-dev-shm-usage']
        )
        try:
            page = browser.new_page()
            inject_api(page)
            ensure_logged_in(browser, page)

            for t_idx, target_url in enumerate(targets, 1):
                try:
                    page.goto(target_url, wait_until='load')
                    inject_api(page)
                    root = resolve_root(page, target_url)
                    files = list_tree(page, root['nodeId'])
                    # 记录本次遍历到的全部非文件夹 uuid
                    for _it in files:
                        _uid = _it.get('id')
                        if _uid:
                            all_seen_uuid_set.add(_uid)

                    def _time_ok(rec: Dict[str, Any]) -> bool:
                        try:
                            ut = rec.get('updatedTime')
                            if ut is None:
                                return False
                            return int(int(ut) / 1000) >= int(update_time)
                        except Exception:
                            return False

                    # 支持 adoc/axls 导出，以及 docx/xlsx/pdf 原格式直下
                    sel = [
                        f for f in files
                        if (
                            (f.get('extension') in ('adoc', 'axls') and f.get('docKey'))
                            or (f.get('extension') in ('docx', 'xlsx', 'pdf'))
                        ) and _time_ok(f)
                    ]
                    log(f"[{t_idx}/{len(targets)}] 目标 {target_url} 可导出: {len(sel)}")
                    all_selected_items.extend(sel)
                except Exception as e:
                    log(f"采集失败: {target_url}: {e}")
                    continue
        finally:
            browser.close()

    # 3. 删除旧文档（先删除映射中这些 uuid 对应的 doc_id）
    uuids_to_update = [it.get('id') for it in all_selected_items if it.get('id')]
    old_doc_ids: List[str] = []
    # 保存更新前的映射状态，用于后续统计（判断新增/更新）
    uuid_is_update_map: Dict[str, bool] = {}  # uuid -> True(更新) / False(新增)
    for uid in uuids_to_update:
        mapping_value = id_map.get(uid)  # 期望为 dict
        doc_id = mapping_value.get('ragflow_doc_id') if isinstance(mapping_value, dict) else None
        file_url = mapping_value.get('ding_doc_url') if isinstance(mapping_value, dict) else None
        if not file_url:
            file_url = f"https://alidocs.dingtalk.com/i/nodes/{uid}"
        uuid_is_update_map[uid] = doc_id is not None  # 如果已有映射，则为更新
        if doc_id:
            old_doc_ids.append(doc_id)
    
    # 统计：删除的旧文档数量
    deleted_before_update_count = len(old_doc_ids)
    if old_doc_ids:
        ok = ragflow_api.delete_documents(old_doc_ids)
        log(f"删除旧文档: {len(old_doc_ids)} -> {'OK' if ok else 'FAIL'}")

    # 4. 导出/上传/映射/解析（单线程）
    uploaded_ids: List[str] = []
    parsed_buffer: List[str] = []
    state = load_json(STATE_PATH, default={'completed': {}})
    
    # 统计信息收集
    stats = {
        'success_count': 0,
        'fail_count': 0,
        'success_items': [],
        'fail_items': [],
        'by_type': {},  # 按文件类型统计
        'by_operation': {'new': 0, 'update': 0},  # 新增/更新
        'ragflow_success': 0,
    }

    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(
            user_data_dir=str(PERSIST_DIR),
            headless=headless,
            args=['--disable-dev-shm-usage']
        )
        try:
            page = browser.new_page()
            inject_api(page)
            ensure_logged_in(browser, page)

            for idx, item in enumerate(all_selected_items, 1):
                item_uuid = item.get('id', '')
                item_name = item.get('name', '')
                item_ext = (item.get('extension') or '').lower()
                # 使用删除前保存的映射状态判断是新增还是更新
                is_update = uuid_is_update_map.get(item_uuid, False)
                
                try:
                    export_and_download(page, browser, item, state, min_unix_ts=update_time)
                    save_json(STATE_PATH, state)
                    # 上传
                    out_path = out_path_for(item)
                    if out_path.exists():
                        doc_id = ragflow_api.upload_document(out_path)
                        # 使用新格式保存映射（包含 ragflow_doc_id 和 ding_doc_url）
                        ding_doc_url = f"https://alidocs.dingtalk.com/i/nodes/{item['id']}"
                        id_map.put_ragflow_mapping(item['id'], doc_id, auto_save=False)

                        # 更新文档元数据，设置 url 字段
                        try:
                            ragflow_api.update_document_metadata(
                                doc_id,
                                meta_fields={"url": ding_doc_url}
                            )
                            log(f"RagFlow 元数据更新成功: {item['id']} -> {ding_doc_url}")
                        except Exception as e:
                            log(f"RagFlow 元数据更新失败: {item['id']} -> {e}")

                        stats['ragflow_success'] += 1

                        uploaded_ids.append(doc_id)
                        parsed_buffer.append(doc_id)
                        
                        # 统计成功
                        stats['success_count'] += 1
                        stats['success_items'].append({
                            'name': item_name,
                            'uuid': item_uuid,
                            'type': item_ext,
                            'doc_id': doc_id
                        })
                        stats['by_type'][item_ext] = stats['by_type'].get(item_ext, 0) + 1
                        if is_update:
                            stats['by_operation']['update'] += 1
                        else:
                            stats['by_operation']['new'] += 1
                        
                        # 清理 PDF
                        try:
                            out_path.unlink(missing_ok=True)
                        except Exception:
                            pass
                        # 触发解析（每 10 个）
                        if len(parsed_buffer) >= 10:
                            try:
                                ragflow_api.parse_documents(parsed_buffer)
                                log(f"[解析成功] 批量 {len(parsed_buffer)} 个文档")
                            except Exception as e:
                                log(f"[解析失败] 批量 {len(parsed_buffer)} 个文档: {e}")
                            parsed_buffer.clear()
                    else:
                        log(f"文件不存在，跳过上传: {out_path}")
                        stats['fail_count'] += 1
                        stats['fail_items'].append({
                            'name': item_name,
                            'uuid': item_uuid,
                            'type': item_ext,
                            'error': '文件不存在'
                        })
                except Exception as e:
                    log(f"处理失败: {item.get('id')} -> {e}")
                    stats['fail_count'] += 1
                    stats['fail_items'].append({
                        'name': item_name,
                        'uuid': item_uuid,
                        'type': item_ext,
                        'error': str(e)
                    })
        finally:
            browser.close()

    # 处理剩余不足10个的文档
    if parsed_buffer:
        try:
            ragflow_api.parse_documents(parsed_buffer)
            log(f"[解析成功] 剩余 {len(parsed_buffer)} 个文档")
        except Exception as e:
            log(f"[解析失败] 剩余 {len(parsed_buffer)} 个文档: {e}")
        parsed_buffer.clear()

    # 5. 同步删除：本地映射中存在但这次没出现的 uuid（与"本次全部遍历到的 uuid"对比）
    current_uuid_set = set(all_seen_uuid_set)
    stale_doc_ids: List[str] = []
    stale_uuids: List[str] = []  # 记录需要删除的 uuid
    for uid, value in list(id_map.items()):
        if uid not in current_uuid_set:
            doc_id = id_map.get_ragflow_doc_id(uid)
            file_url = value.get('ding_doc_url') if isinstance(value, dict) else None
            if not file_url:
                file_url = f"https://alidocs.dingtalk.com/i/nodes/{uid}"
            if doc_id:
                stale_doc_ids.append(doc_id)
            stale_uuids.append(uid)
    
    # 统计：同步删除的数量
    sync_deleted_count = len(stale_doc_ids)
    if stale_doc_ids:
        ok = ragflow_api.delete_documents(stale_doc_ids)
        log(f"同步删除远端: {len(stale_doc_ids)} -> {'OK' if ok else 'FAIL'}")
        if ok:
            # 删除本地映射
            for uid in stale_uuids:
                id_map.delete(uid, auto_save=False)

    # 6. 保存映射
    id_map.save()

    # 7. 统计
    after_keys = set(k for k, _ in list(id_map.items()))
    total_deleted = deleted_before_update_count + sync_deleted_count
    
    # 生成并发送详细的统计消息
    _send_statistics_notification(
        update_time=update_time,
        targets=targets,
        stats=stats,
        before_keys_count=len(before_keys),
        after_keys_count=len(after_keys),
        total_selected=len(all_selected_items),
        deleted_before_update=deleted_before_update_count,
        sync_deleted=sync_deleted_count,
        total_deleted=total_deleted,
        uploaded_count=len(uploaded_ids),
        parsed_count=len(uploaded_ids)  # 所有上传的都会解析
    )
    
    log(f"run_update 完成：选中 {len(all_selected_items)}，上传 {len(uploaded_ids)}，映射计数 {len(after_keys)}（原 {len(before_keys)}）")

def _send_statistics_notification(
    update_time: int,
    targets: List[str],
    stats: Dict[str, Any],
    before_keys_count: int,
    after_keys_count: int,
    total_selected: int,
    deleted_before_update: int,
    sync_deleted: int,
    total_deleted: int,
    uploaded_count: int,
    parsed_count: int
) -> None:
    """生成并发送详细的统计消息到钉钉"""
    if not ROBOT_ACCESS_TOKEN or not ROBOT_SECRET:
        log("未配置钉钉机器人，跳过统计通知")
        return
    
    try:
        # 日期和时间范围
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d %H:%M:%S")
        update_datetime = datetime.fromtimestamp(update_time)
        
        # 构建 Markdown 消息
        lines = []
        lines.append(f"#### 📊 钉钉文档同步统计报告")
        lines.append("")
        lines.append(f"**执行时间**: {date_str}")
        lines.append("")
        lines.append("---")
        lines.append("")
        
        # 目标统计
        lines.append("##### 📁 目标目录")
        for idx, target in enumerate(targets, 1):
            # 截断过长的 URL
            target_display = target if len(target) <= 80 else target[:77] + "..."
            lines.append(f"{idx}. `{target_display}`")
        lines.append("")
        
        # 总体统计
        lines.append("##### 📈 上传统计")
        lines.append(f"- **成功处理**: {stats['success_count']} 个")
        lines.append(f"- **处理失败**: {stats['fail_count']} 个")
        lines.append(f"- **RagFlow 任务**: {stats['ragflow_success']} 个")
        lines.append("")
        
        # 操作类型统计
        lines.append("##### 🔄 操作类型")
        lines.append(f"- **新增文档**: {stats['by_operation']['new']} 个")
        lines.append(f"- **更新文档**: {stats['by_operation']['update']} 个")
        lines.append(f"- **删除文档**: {sync_deleted} 个")
        lines.append("")
        
        markdown_text = "\n".join(lines)
        
        # 发送钉钉消息
        send_dingtalk_markdown(
            ROBOT_ACCESS_TOKEN,
            ROBOT_SECRET,
            "钉钉文档同步统计报告",
            markdown_text
        )
        
        log("统计消息已发送到钉钉")
    except Exception as e:
        log(f"发送统计消息失败: {e}")


def log(message: str) -> None:
    try:
        # print(f"[PRC] {message}")
        try:
            logging.info(message)
        except Exception:
            pass
    except Exception:
        pass

def _mask_token(value: str) -> str:
    try:
        if not value:
            return "<empty>"
        v = str(value)
        if len(v) <= 8:
            return "*" * max(1, len(v) - 2) + v[-2:]
        return v[:4] + "..." + v[-4:]
    except Exception:
        return "<masked>"


def inject_api(page: Page) -> None:
    log("注入 tiny_alidocs_api.js ...")
    content = INJECT_FILE.read_text(encoding='utf-8')
    # 正确参数名为 script（Python Playwright）
    page.add_init_script(script=content)
    # 确保在当前已加载页面也可立即生效
    try:
        page.evaluate("() => { if (!window.alidocs) {" + content + "} }")
        log("注入完成并已在当前页生效")
    except Exception:
        log("注入完成（当前页生效校验略过）")


def _setup_incremental_logging() -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        fname = f"incremental_{datetime.now().strftime('%Y%m%d')}.log"
        fpath = LOG_DIR / fname
        logger = logging.getLogger()
        logger.setLevel(logging.INFO)
        # 清理已有处理器，避免重复写入
        if not any(isinstance(h, logging.FileHandler) and getattr(h, 'baseFilename', '').endswith(str(fpath)) for h in logger.handlers):
            logger.handlers = []
            fh = logging.FileHandler(str(fpath), encoding='utf-8')
            sh = logging.StreamHandler(sys.stdout)
            fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
            fh.setFormatter(fmt)
            sh.setFormatter(fmt)
            logger.addHandler(fh)
            logger.addHandler(sh)
        # 清理30天前的日志
        try:
            cutoff = time.time() - 30 * 24 * 3600
            for entry in LOG_DIR.iterdir():
                try:
                    if entry.is_file() and entry.name.startswith('incremental_') and entry.suffix == '.log':
                        if entry.stat().st_mtime < cutoff:
                            entry.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception:
            pass
    except Exception:
        pass


# ======== 登录与机器人通知：复用 simple_test.py 思路（同步版） ========
DING_LOGIN_URL = os.getenv(
    "DING_LOGIN_URL",
    "https://login.dingtalk.com/oauth2/challenge.htm?redirect_uri=https%3A%2F%2Falidocs.dingtalk.com%2Fi%2F%3Fspm%3Da2q1e.24441682.0.0.2c8b252137UE4J&response_type=none&client_id=dingoaxhhpeq7is6j1sapz&scope=openid",
)
ROBOT_ACCESS_TOKEN = os.getenv("DING_ROBOT_ACCESS_TOKEN", "c47714769baf275fa072ad78169f0c9bfc96612e74ec08eee0b111f1804eea76")
ROBOT_SECRET = os.getenv("DING_ROBOT_SECRET", "SEC7e1604e3cf35b1a4543f8acf1750dae2946e3ee48f601d7672489734be0c7e98")
PICUI_TOKEN = os.getenv("PICUI_TOKEN", "1795|GdzkBaU9wreWyuhYls9Y06WUQZ3mGB7b1aQrDp7e")
PICUI_API = os.getenv("PICUI_API", "https://picui.cn/api/v1")
AT_MOBILES_ENV = os.getenv("AT_MOBILES", "")
AT_MOBILES_LIST = [m.strip() for m in AT_MOBILES_ENV.split(',') if m.strip()]

AT_USER_IDS_ENV = os.getenv("AT_USER_IDS", "")
AT_USER_IDS_LIST = [u.strip() for u in AT_USER_IDS_ENV.split(',') if u.strip()]
TRIGGER_BASE_URL = os.getenv("TRIGGER_BASE_URL", "http://localhost:8999")

QR_LOGIN_SELECTOR = 'text="扫码登录"'
TARGET_SELECTORS = [
    'div.module-qrcode-op-line div.base-comp-check-box-rememberme-box.dingtalk-login-iconfont.dingtalk-login-icon-checkbox-undone',
    'div.module-qrcode-op-line-with-open-passkey div.base-comp-check-box-rememberme-box.dingtalk-login-iconfont.dingtalk-login-icon-checkbox-undone',
    'div.module-qrcode-op-item div.base-comp-check-box-rememberme-box.dingtalk-login-iconfont.dingtalk-login-icon-checkbox-undone',
]


def _post_with_log(url, **kwargs):
    try:
        resp = requests.post(url, timeout=kwargs.pop("timeout", 20), **kwargs)
        return resp
    except Exception as e:
        raise RuntimeError(f"POST {url} failed: {e}")


def _put_with_log(url, data, timeout=30):
    try:
        resp = requests.put(url, data=data, timeout=timeout)
        return resp
    except Exception as e:
        raise RuntimeError(f"PUT {url} failed: {e}")


def send_custom_robot_group_message(access_token: str, secret: str, msg: str, at_user_ids=None, at_mobiles=None, is_at_all: bool = False):
    """发送钉钉自定义机器人群消息（ActionCard）。
    参考用户提供的示例实现。
    """
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(secret.encode('utf-8'), string_to_sign.encode('utf-8'), digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))

    url = f"https://oapi.dingtalk.com/robot/send?access_token={access_token}&timestamp={timestamp}&sign={sign}"

    body = {
        "at": {
            "isAtAll": str(is_at_all).lower(),
            "atUserIds": at_user_ids or AT_USER_IDS_LIST,
            "atMobiles": at_mobiles or AT_MOBILES_LIST,
        },
        "msgtype": "actionCard",
        "actionCard": {
            "title": "登录过期",
            "text": msg or "登录过期，钉钉文档自动化脚本运行失败! \n\n请对应负责人重新扫码登录。",
            "btnOrientation": "0",
            "btns": [
                {
                    "title": "扫码登录",
                    "actionURL": f"{TRIGGER_BASE_URL}/start-login?token=abc"
                }
            ]
        }
    }
    headers = {'Content-Type': 'application/json'}
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=15)
        _ = resp.text
    except Exception:
        pass


def send_dingtalk_markdown(access_token: str, secret: str, title: str, text: str, at_mobiles=None):
    """发送钉钉 Markdown 消息"""
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(secret.encode('utf-8'), string_to_sign.encode('utf-8'), digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    
    url = f"https://oapi.dingtalk.com/robot/send?access_token={access_token}&timestamp={timestamp}&sign={sign}"
    
    body = {
        "msgtype": "markdown",
        "markdown": {
            "title": title,
            "text": text
        },
        "at": {
            "isAtAll": False,
            "atMobiles": at_mobiles or AT_MOBILES_LIST,
        }
    }
    headers = {'Content-Type': 'application/json'}
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=15)
        _ = resp.text
    except Exception as e:
        log(f"发送钉钉消息失败: {e}")


def upload_image_return_url(image_path: str) -> str:
    headers = {"Accept": "application/json"}
    if PICUI_TOKEN:
        headers["Authorization"] = f"Bearer {PICUI_TOKEN}"
    try:
        with open(image_path, "rb") as f:
            resp = _post_with_log(
                f"{PICUI_API}/upload",
                headers=headers,
                files={"file": (os.path.basename(image_path), f, "image/png")},
                timeout=25,
            )
        if resp.headers.get("Content-Type", "").startswith("application/json"):
            data = resp.json()
        else:
            data = {}
        if data.get("status") and data.get("data"):
            url = data["data"].get("url") or (data["data"].get("links", {}) or {}).get("url")
            if url:
                return url
    except Exception:
        pass
    # 退化 0x0.st
    try:
        with open(image_path, "rb") as f:
            resp = requests.post("https://0x0.st", files={"file": f}, timeout=15)
        if resp.ok and resp.text.strip().startswith("http"):
            return resp.text.strip()
    except Exception:
        pass
    raise RuntimeError("无法上传图片到公共图床")


def sign_robot_request(secret: str):
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return timestamp, sign


def send_dingtalk_markdown_image(access_token: str, secret: str, title: str, image_url: str, extra_text: str = "", at_mobiles=None):
    ts, sign = sign_robot_request(secret)
    url = f"https://oapi.dingtalk.com/robot/send?access_token={access_token}&timestamp={ts}&sign={sign}"
    text_lines = []
    if extra_text:
        text_lines.append(extra_text)
    text_lines.append(f"![screenshot]({image_url})")
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": "\n\n".join(text_lines)},
        "at": {"isAtAll": False, "atMobiles": at_mobiles or []},
    }
    resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=15)
    try:
        _ = resp.json()
    except Exception:
        pass


def ensure_logged_in(context: BrowserContext, page: Page):
    """仅检查是否已登录。
    - 已登录：继续
    - 未登录：发送 ActionCard 并终止
    不再在 main.py 中执行任何登录相关的 UI 操作或截图。
    """
    log("登录检查：访问桌面页")
    try:
        page.goto("https://alidocs.dingtalk.com/i/desktop", wait_until="domcontentloaded", timeout=10000)
        if "alidocs.dingtalk.com/i/desktop" in page.url:
            log("已登录")
            return
        log("未登录，发送扫码登录通知并终止")
        if ROBOT_ACCESS_TOKEN and ROBOT_SECRET:
            try:
                send_custom_robot_group_message(
                    ROBOT_ACCESS_TOKEN,
                    ROBOT_SECRET,
                    "登录过期，钉钉文档自动化脚本运行失败! \n\n请对应负责人重新扫码登录。",
                    at_user_ids=None,
                    at_mobiles=AT_MOBILES_LIST,
                    is_at_all=False,
                )
            except Exception as e:
                log("发送 ActionCard 失败: " + str(e))
        raise SystemExit(2)
    except Exception as e:
        log(f"桌面页访问失败: {e}")
        # 失败时按未登录处理
        if ROBOT_ACCESS_TOKEN and ROBOT_SECRET:
            try:
                send_custom_robot_group_message(
                    ROBOT_ACCESS_TOKEN,
                    ROBOT_SECRET,
                    "无法访问桌面页，可能未登录或网络异常。请扫码登录后重试。",
                    at_user_ids=None,
                    at_mobiles=AT_MOBILES_LIST,
                    is_at_all=False,
                )
            except Exception as e2:
                log("发送 ActionCard 失败: " + str(e2))
        raise SystemExit(2)


def call_api(page: Page, fn: str, *args):
    # Playwright Python evaluate 只接受一个可选参数，这里通过对象打包传入
    return page.evaluate(
        "(params) => window.alidocs[params.fn](...(params.args || []))",
        {"fn": fn, "args": list(args)}
    )


def resolve_root(page: Page, url_or_id: str) -> Dict[str, Any]:
    log(f"解析根节点: {url_or_id}")
    r = call_api(page, 'resolveNode', url_or_id)
    if not r.get('ok'):
        raise RuntimeError(f"resolveNode failed: {r}")
    data = r['data']
    log(f"根节点解析成功: nodeId={data.get('nodeId')} type={data.get('type')}")
    return data


def list_tree(page: Page, root_id: str) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    stack: List[Dict[str, Any]] = [{ 'id': root_id, 'rel': [] }]

    while stack:
        cur = stack.pop()
        parent_id = cur['id']
        # 列举当前父节点
        # log(f"列举: rel={'/'.join(cur['rel']) or '/'}")
        cursor: Optional[str] = None
        seen_cursors = set()
        page_count = 0
        while True:
            r = call_api(page, 'listChildren', parent_id, cursor)
            if not r.get('ok'):
                raise RuntimeError(f"listChildren failed for {parent_id}: {r}")
            data = r['data']
            items = data.get('items') or []
            # 可按需打开分页日志
            for it in items:
                typ = it.get('type')
                name = sanitize_name(it.get('name') or '')
                const_has_children = bool(it.get('hasChildren'))
                # 支持“文件也可能有下级”：有下级就入栈继续遍历
                if typ == 'folder' or const_has_children:
                    stack.append({ 'id': it['id'], 'rel': cur['rel'] + [name] })
                # 非文件夹（包括有下级的文件本身）都应计入结果，供导出
                if typ != 'folder':
                    results.append({
                        'id': it['id'],
                        'type': typ,
                        'name': name,
                        'rel': cur['rel'],
                        'contentType': it.get('contentType'),
                        'docKey': it.get('docKey'),
                        'dentryKey': it.get('dentryKey'),
                        'extension': it.get('extension'),
                        'updatedTime': it.get('updatedTime'),
                        'uuid': it.get('id')
                    })
            next_cursor = data.get('nextCursor')
            # 终止条件：无 next、items 为空、或 cursor 未推进、或页数过多
            if not next_cursor:
                break
            if len(items) == 0:
                break
            if next_cursor == cursor or next_cursor in seen_cursors:
                break
            cursor = next_cursor
            seen_cursors.add(cursor)
            page_count += 1
            if page_count >= 200:
                break
    log(f"总文件数: {len(results)}")
    # 文件清单（含相对路径）
    try:
        for idx, r in enumerate(results, 1):
            relpath = "/".join([*r['rel'], r['name']]) if r.get('rel') else r['name']
            ext = r.get('extension') or (r.get('contentType') or '')
            if idx <= 50:
                log(f"  [{idx}] {relpath}  type={r.get('type')} ext={ext} updatedTime={r.get('updatedTime')} uuid={r.get('uuid')}")
            elif idx == 51:
                log("  ... 省略后续条目 ...")
    except Exception:
        pass
    # 统计类型分布
    dist: Dict[str, int] = {}
    for r in results:
        dist[r['type']] = dist.get(r['type'], 0) + 1
    log("类型分布: " + ", ".join([f"{k}={v}" for k,v in dist.items()]))
    return results


def ext_for_item(item: Dict[str, Any]) -> Optional[str]:
    """决定导出/下载的目标扩展名：
    - adoc → pdf（导出）
    - axls → xlsx（导出）
    - 原始上传的 docx/xlsx/pdf → 原格式直下
    其他返回 None（跳过）。
    """
    ext = (item.get('extension') or '').lower()
    if ext in ('docx', 'xlsx', 'pdf'):
        return ext
    itype = item.get('type')
    if itype == 'doc':
        return 'pdf'
    if itype == 'sheet':
        return 'xlsx'
    return None


def out_path_for(item: Dict[str, Any]) -> Path:
    """返回下载文件的输出路径：
    - 扁平目录：所有文件直接保存在 TMP_DIR 下
    - 文件名使用 name（清理后的名称），而不是 uuid
    """
    ext = ext_for_item(item)
    ensure_dir(TMP_DIR)
    # 使用清理后的名称作为文件名
    name = sanitize_name(item.get('name', 'untitled'), max_len=200)
    filename = f"{name}.{ext}"
    return TMP_DIR / filename


def export_and_download(page: Page, ctx: BrowserContext, item: Dict[str, Any], state: Dict[str, Any], min_unix_ts: Optional[int] = None):
    ext = ext_for_item(item)
    if not ext:
        return
    out_path = out_path_for(item)
    if file_exists_nonempty(out_path):
        # 已存在则删除，强制覆盖
        try:
            out_path.unlink(missing_ok=True)
            log(f"已存在，删除并覆盖: {out_path}")
        except Exception as e:
            log(f"删除已存在文件失败: {out_path}, {e}")

    download_url: Optional[str] = None
    # 分支：原始文件 docx/xlsx/pdf 直链下载
    if (item.get('extension') or '').lower() in ('docx', 'xlsx', 'pdf'):
        log(f"直链下载原始文件: {item['name']} -> {ext}")
        last_err = None
        for d in backoff_delays():
            try:
                r = call_api(page, 'downloadDocument', item['id'])
            except Exception as e:
                last_err = str(e)
                log(f"  获取直链异常: {last_err}，重试{d}s")
                sleep(d)
                continue
            if r.get('ok') and r['data'].get('url'):
                download_url = r['data']['url']
                break
            last_err = r.get('error') or 'unknown error'
            log(f"  获取直链失败: {last_err}，重试{d}s")
            sleep(d)
        if not download_url:
            raise RuntimeError(f"downloadDocument failed: {item['name']}: {last_err}")
    else:
        # 分支：adoc/axls 导出任务
        log(f"创建导出任务: {item['name']} -> {ext}")
        last_err = None
        for d in backoff_delays():
            try:
                r = call_api(page, 'createExportTask', item['id'], ext)
            except Exception as e:
                last_err = str(e)
                log(f"  创建异常: {last_err}，重试{d}s")
                sleep(d)
                continue
            if r.get('ok') and r['data'].get('taskId'):
                task_id = r['data']['taskId']
                log(f"  任务已创建: {task_id}")
                break
            last_err = r.get('error') or 'unknown error'
            log(f"  创建失败: {last_err}，重试{d}s")
            sleep(d)
        else:
            raise RuntimeError(f"createExportTask failed: {item['name']}: {last_err}")

        # 轮询任务
        for i in range(30):
            r = call_api(page, 'getExportTask', task_id)
            if not r.get('ok'):
                if (i + 1) % 5 == 0:
                    log(f"  轮询失败({i+1})")
                sleep(2)
                continue
            data = r['data']
            st = str(data.get('status'))
            if st.lower() == 'success' and data.get('downloadUrl'):
                download_url = data['downloadUrl']
                log("  导出完成")
                break
            if st.lower() == 'failed':
                raise RuntimeError(f"export failed: {data}")
            sleep(2)
        if not download_url:
            raise RuntimeError("export timeout")

    # 下载（带 Cookie）
    ensure_dir(out_path.parent)
    attempt = 0
    for d in backoff_delays():
        attempt += 1
        resp = ctx.request.get(download_url)
        if resp.ok:
            with open(out_path, 'wb') as f:
                f.write(resp.body())
            if file_exists_nonempty(out_path):
                state.setdefault('completed', {})[item['id']] = { 'file': str(out_path) }
                log(f"下载完成: {item['name']}")
                return
        if attempt >= 3:
            log("下载失败，重试中")
        sleep(d)
    raise RuntimeError(f"download failed: {download_url}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--url', required=False, action='append', help='目录链接或节点ID，可重复传；如未提供将从环境变量 TARGET_URLS/TARGET_URL 读取')
    ap.add_argument('--headless', required=False, choices=['true','false'], default=os.getenv('HEADLESS', 'true').lower())
    ap.add_argument('--min_ts', required=False, type=int, help='只导出更新时间（秒）>= 该值的文档；也可用环境变量 MIN_TS 指定')
    ap.add_argument('--mode', required=False, choices=['full','incremental'], help='运行模式：full（全量）或 incremental（增量）')
    args = ap.parse_args()

    # 若指定运行模式，直接执行并返回
    if args.mode == 'full':
        run_full_update()
        return
    if args.mode == 'incremental':
        run_incremental_update()
        return

    # 汇总目标列表：优先命令行 --url（可多次），否则读取环境变量
    target_list: List[str] = []
    if args.url:
        target_list.extend([u for u in args.url if str(u).strip()])
    else:
        env_urls = os.getenv('TARGET_URLS', '') or ''
        if env_urls.strip():
            # 支持逗号、分号、换行分隔
            raw = [x for part in env_urls.split('\n') for x in part.split(',')]
            raw2 = [x for part in raw for x in part.split(';')]
            target_list.extend([x.strip() for x in raw2 if x.strip()])
        else:
            single = os.getenv('TARGET_URL', '').strip()
            if single:
                target_list.append(single)

    if not target_list:
        log('未提供任何目标：--url / TARGET_URLS / TARGET_URL 均为空，退出。')
        raise SystemExit(2)

    headless = str(args.headless).lower() == 'true'
    env_min_ts = os.getenv('MIN_TS')
    min_ts: Optional[int] = None
    if args.min_ts is not None:
        min_ts = int(args.min_ts)
    elif env_min_ts is not None and str(env_min_ts).strip() != '':
        try:
            min_ts = int(str(env_min_ts).strip())
        except Exception:
            min_ts = None
    log(f"启动: headless={headless}")
    ensure_dir(TMP_DIR)
    ensure_dir(PERSIST_DIR)
    log(f"输出: {TMP_DIR}")

    state = load_json(STATE_PATH, default={ 'completed': {} })

    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(
            user_data_dir=str(PERSIST_DIR),
            headless=headless,
            args=['--disable-dev-shm-usage']
        )
        try:
            page = browser.new_page()
            inject_api(page)
            # 登录检查
            log("登录检查 ...")
            ensure_logged_in(browser, page)

            for t_idx, target_url in enumerate(target_list, 1):
                try:
                    page.goto(target_url, wait_until='load')
                    log(f"打开目标[{t_idx}/{len(target_list)}]: {target_url}")

                    # 再次确保注入生效（针对中途跳转）
                    inject_api(page)

                    root = resolve_root(page, target_url)
                    files = list_tree(page, root['nodeId'])

                    # 支持 adoc/axls 导出，以及 docx/xlsx/pdf 原格式直下；且满足最小更新时间（毫秒转秒比较）
                    def _time_ok(rec: Dict[str, Any]) -> bool:
                        if min_ts is None:
                            return True
                        try:
                            ut = rec.get('updatedTime')
                            if ut is None:
                                return False
                            return int(int(ut) / 1000) >= int(min_ts)
                        except Exception:
                            return False

                    sel = [
                        f for f in files
                        if (
                            (f.get('extension') in ('adoc', 'axls') and f.get('docKey'))
                            or (f.get('extension') in ('docx', 'xlsx', 'pdf'))
                        ) and _time_ok(f)
                    ]
                    log(f"导出目标: {len(sel)}")

                    for idx, item in enumerate(sel, 1):
                        try:
                            log(f"[{idx}/{len(sel)}] {item['name']} -> {item.get('id')}")
                            export_and_download(page, browser, item, state, min_unix_ts=min_ts)
                            save_json(STATE_PATH, state)
                            print(f"[{idx}/{len(sel)}] OK: {item['id']}")
                        except Exception as e:
                            print(f"[{idx}/{len(sel)}] FAIL: {item.get('id')} -> {e}")
                            save_json(STATE_PATH, state)
                except Exception as e:
                    log(f"目标处理失败: {target_url}: {e}")
                    continue

        finally:
            browser.close()


if __name__ == '__main__':
    sys.exit(main())



