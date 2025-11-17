import os
import sys
import time
import hmac
import base64
import hashlib
import urllib.parse
import shutil
from pathlib import Path
import logging
from datetime import datetime, timedelta
import subprocess
from typing import Any, Dict, Optional, List

import requests
from playwright.sync_api import sync_playwright, BrowserContext, Page


def log(message: str) -> None:
    try:
        # print(f"[login-runner] {message}")
        try:
            logging.info(message)
        except Exception:
            pass
    except Exception:
        pass


def ensure_dir(p: Path) -> None:
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


# === 环境变量（与 main.py 一致） ===
DING_LOGIN_URL = os.getenv(
    "DING_LOGIN_URL",
    "https://login.dingtalk.com/oauth2/challenge.htm?redirect_uri=https%3A%2F%2Falidocs.dingtalk.com%2Fi%2F%3Fspm%3Da2q1e.24441682.0.0.2c8b252137UE4J&response_type=none&client_id=dingoaxhhpeq7is6j1sapz&scope=openid",
)
ROBOT_ACCESS_TOKEN = os.getenv("DING_ROBOT_ACCESS_TOKEN", "")
ROBOT_SECRET = os.getenv("DING_ROBOT_SECRET", "")
PICUI_TOKEN = os.getenv("PICUI_TOKEN", "")
PICUI_API = os.getenv("PICUI_API", "https://picui.cn/api/v1")
SMMS_TOKEN = os.getenv("SMMS_TOKEN", "")

# Playwright 持久化目录（通过挂载实现跨容器共享登录态）
USER_DATA_DIR = os.getenv("USER_DATA_DIR", "/app/persistent_context/Default")
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
DEBUG_SHOTS = os.getenv("DEBUG_SHOTS", "false").lower() == "true"
CLEAR_USER_DATA = os.getenv("CLEAR_USER_DATA", "false").lower() == "true"
TRY_DESKTOP_TIMEOUT_S = int(os.getenv("TRY_DESKTOP_TIMEOUT_S", "90"))

HERE = Path(__file__).resolve().parent
# 截图目录统一放在 /data/screenshot 下
TEST_DIR = Path('/data/screenshot')
LOGIN_LOG_DIR = Path('/data/log/login')


def _setup_login_logging() -> None:
    try:
        LOGIN_LOG_DIR.mkdir(parents=True, exist_ok=True)
        fname = f"login_{datetime.now().strftime('%Y%m%d')}.log"
        fpath = LOGIN_LOG_DIR / fname
        logger = logging.getLogger()
        logger.setLevel(logging.INFO)
        if not any(isinstance(h, logging.FileHandler) and getattr(h, 'baseFilename', '').endswith(str(fpath)) for h in logger.handlers):
            logger.handlers = []
            fh = logging.FileHandler(str(fpath), encoding='utf-8')
            sh = logging.StreamHandler(sys.stdout)
            fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
            fh.setFormatter(fmt)
            sh.setFormatter(fmt)
            logger.addHandler(fh)
            logger.addHandler(sh)
        # 清理30天前
        try:
            cutoff = time.time() - 30 * 24 * 3600
            for entry in LOGIN_LOG_DIR.iterdir():
                try:
                    if entry.is_file() and entry.name.startswith('login_') and entry.suffix == '.log':
                        if entry.stat().st_mtime < cutoff:
                            entry.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception:
            pass
    except Exception:
        pass

QR_LOGIN_SELECTOR = 'text="QR Code"'
TARGET_SELECTORS: List[str] = [
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


def upload_image_return_url(image_path: str) -> str:
    headers = {"Accept": "application/json"}
    if PICUI_TOKEN:
        headers["Authorization"] = f"Bearer {PICUI_TOKEN}"
    try:
        # 计算7天后的过期时间，格式：yyyy-MM-dd HH:mm:ss
        expired_at = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        
        with open(image_path, "rb") as f:
            resp = _post_with_log(
                f"{PICUI_API}/upload",
                headers=headers,
                files={"file": (os.path.basename(image_path), f, "image/png")},
                data={
                    "album_id": 1905,
                    "expired_at": expired_at
                },
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
    # 次级：sm.ms（可匿名或使用 token）
    try:
        files = {"smfile": (os.path.basename(image_path), open(image_path, "rb"), "image/png")}
        headers2 = {}
        if SMMS_TOKEN:
            headers2["Authorization"] = SMMS_TOKEN
        resp = requests.post("https://sm.ms/api/v2/upload", headers=headers2, files=files, timeout=20)
        if resp.headers.get("Content-Type", "").startswith("application/json"):
            data = resp.json()
            if data.get("success") and data.get("data", {}).get("url"):
                return data["data"]["url"]
    except Exception:
        pass

    # 退化：0x0.st
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
    try:
        requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=15)
    except Exception:
        pass


def send_dingtalk_text(access_token: str, secret: str, text: str, at_mobiles=None):
    ts, sign = sign_robot_request(secret)
    url = f"https://oapi.dingtalk.com/robot/send?access_token={access_token}&timestamp={ts}&sign={sign}"
    payload = {
        "msgtype": "text",
        "text": {"content": text},
        "at": {"isAtAll": False, "atMobiles": at_mobiles or []},
    }
    try:
        requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=15)
    except Exception:
        pass


def ensure_logged_in(context: BrowserContext, page: Page):
    initial_png = TEST_DIR / "initial_page.png"
    after_qr_png = TEST_DIR / "after_qr_login_click.png"
    after_click_png = TEST_DIR / "after_click.png"
    error_png = TEST_DIR / "error_page.png"
    already_png = TEST_DIR / "already_logged_in.png"
    ensure_dir(TEST_DIR)

    def send_step_snapshot(path: Path, title: str, extra: str = "", critical: bool = False):
        if not (critical or DEBUG_SHOTS):
            return
        try:
            page.screenshot(path=str(path), full_page=True)
        except Exception:
            pass
        if ROBOT_ACCESS_TOKEN and ROBOT_SECRET:
            try:
                url = upload_image_return_url(str(path))
                send_dingtalk_markdown_image(ROBOT_ACCESS_TOKEN, ROBOT_SECRET, title, url, extra_text=extra)
            except Exception as e:
                log("机器人通知失败: " + str(e))

    # 先检查是否已在桌面页
    log("登录检查：访问桌面页")
    try:
        page.goto("https://alidocs.dingtalk.com/i/desktop", wait_until="domcontentloaded", timeout=15000)
        if "alidocs.dingtalk.com/i/desktop" in page.url:
            log("已登录")
            # send_step_snapshot(already_png, "钉钉 - 已登录", "钉钉文档登录未过期！")
            send_dingtalk_text(ROBOT_ACCESS_TOKEN, ROBOT_SECRET, "钉钉文档登录未过期，请勿重复点击 \"扫码登录\" 按钮！")
            return
    except Exception as e:
        log(f"桌面页访问失败: {e}")
    send_dingtalk_text(ROBOT_ACCESS_TOKEN, ROBOT_SECRET, "获取二维码中，请稍后...")
    # 跳到登录页并截图
    log("打开登录页")
    try:
        page.goto(DING_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        log(f"登录页异常: {e}")
    page.wait_for_timeout(2000)
    send_step_snapshot(initial_png, "钉钉 - 登录截图", "初始页面")

    # 点击“扫码登录”并截图
    clicked_qr = False
    # 更宽松的候选选择器
    qr_selectors: List[str] = [
        QR_LOGIN_SELECTOR,
        "text=扫码登录"
    ]
    try:
        found = False
        for sel in qr_selectors:
            try:
                loc = page.locator(sel)
                if loc.count() > 0:
                    loc.first.click()
                    found = True
                    break
            except Exception:
                continue
        if not found:
            page.wait_for_selector(QR_LOGIN_SELECTOR, timeout=8000)
            page.click(QR_LOGIN_SELECTOR)
        clicked_qr = True
        page.wait_for_timeout(1200)
    except Exception as e:
        log(f"扫码登录点击失败: {e}")
    send_step_snapshot(after_qr_png if clicked_qr else error_png, "钉钉 - 登录截图", "点击扫码后/失败页")

    # 点击二维码区域“自动登录”复选框（多选择器容错）并截图
    clicked_auto = False
    for sel in TARGET_SELECTORS:
        try:
            page.wait_for_selector(sel, timeout=5000)
            page.click(sel)
            clicked_auto = True
            break
        except Exception:
            continue
    page.wait_for_timeout(800)
    send_step_snapshot(after_click_png if clicked_auto else error_png, "钉钉 - 登录截图", "自动登录勾选后/失败页")

    # 额外发送“请扫码登录”提示
    # 关键提示：请扫码登录（非调试也会发送一次）
    try:
        pic_to_send = after_click_png if clicked_auto else (after_qr_png if clicked_qr else initial_png)
        send_step_snapshot(pic_to_send, "钉钉 - 登录截图", f"请扫描二维码登录（有效{TRY_DESKTOP_TIMEOUT_S}秒）", critical=True)
    except Exception:
        pass

    # 等待跳转到桌面页（最长120秒）并触发页面活动
    log("等待跳转到桌面页")
    deadline_ms = TRY_DESKTOP_TIMEOUT_S * 1000
    start = time.time()
    reached = False
    while (time.time() - start) * 1000 < deadline_ms:
        try:
            if "alidocs.dingtalk.com/i/desktop" in page.url:
                reached = True
                break
            page.wait_for_url("**://alidocs.dingtalk.com/i/desktop*", timeout=2000)
            reached = True
            break
        except Exception:
            page.wait_for_timeout(1000)
    if reached:
        log("登录成功，已到达桌面页")
        try:
            page.wait_for_timeout(2000)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(800)
            page.evaluate("window.scrollTo(0, 0)")
            page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            page.evaluate("document.dispatchEvent(new Event('focus'))")
        except Exception:
            pass
        try:
            img = after_click_png if clicked_auto else (after_qr_png if clicked_qr else initial_png)
            # 关键提示：已登录（非调试也会发送一次）
            send_step_snapshot(img, "钉钉 - 已登录", "登录成功", critical=True)
        except Exception:
            pass

        # 发送“重新调用自动更新脚本”的文本提示（不带图片）
        try:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if ROBOT_ACCESS_TOKEN and ROBOT_SECRET:
                send_dingtalk_text(
                    ROBOT_ACCESS_TOKEN,
                    ROBOT_SECRET,
                    f"{now_str}\n重新调用自动更新脚本",
                    at_mobiles=None,
                )
        except Exception as e:
            log("发送调度文本提示失败: " + str(e))

        # 调用 main.py 执行一次下载（需要 TARGET_URL 环境变量）
        try:
            urls_env = os.getenv("TARGET_URLS", "") or ""
            target_url = os.getenv("TARGET_URL", "").strip()
            has_targets = bool(target_url) or bool(urls_env.strip())
            if not has_targets:
                log("未设置 TARGET_URL/TARGET_URLS，跳过自动下载触发")
            else:
                log("触发一次自动下载: PRC.main --mode incremental")
                # 直接依赖容器环境中的 .env（TARGET_URLS/TARGET_URL、HEADLESS）
                # 使用模块方式运行以支持相对导入
                env = os.environ.copy()
                env['PYTHONPATH'] = env.get('PYTHONPATH', '') + (":" if env.get('PYTHONPATH') else "") + "/app"
                subprocess.run([
                    sys.executable, "-m", "PRC.main", "--mode", "incremental"
                ], cwd="/app", check=False, env=env)
        except Exception as e:
            log("触发自动下载失败: " + str(e))
    else:
        log("跳转超时")
        if DEBUG_SHOTS:
            send_step_snapshot(error_png, "钉钉 - 登录截图", "二维码超时，请重新获取登录二维码。")
        else:
            if ROBOT_ACCESS_TOKEN and ROBOT_SECRET:
                send_dingtalk_text(ROBOT_ACCESS_TOKEN, ROBOT_SECRET, "二维码超时，请重新获取登录二维码。")


def main() -> int:
    _setup_login_logging()
    # 可选：清理持久化目录（强制每次登录）
    udd = Path(USER_DATA_DIR)
    if CLEAR_USER_DATA and udd.exists():
        try:
            # 不删除挂载点本身，仅清空其下内容，避免“Device or resource busy”
            for entry in udd.iterdir():
                try:
                    if entry.is_dir():
                        shutil.rmtree(entry)
                    else:
                        entry.unlink(missing_ok=True)
                except Exception as ie:
                    log(f"清理项失败: {entry}: {ie}")
            log(f"已清空持久化目录内容: {udd}")
        except Exception as e:
            log(f"清理持久化目录失败: {e}")
    # 持久化目录确保存在（由卷挂载提供）
    ensure_dir(udd)

    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=HEADLESS,
            args=['--disable-dev-shm-usage']
        )
        try:
            page = browser.new_page()
            ensure_logged_in(browser, page)
        finally:
            # 关闭浏览器以确保 Playwright 将 session 写回 USER_DATA_DIR（持久化）
            browser.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())


