from flask import Flask, request, redirect, make_response
import os
import logging
import subprocess
import sys
import threading

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# 全局锁：防止并发启动多个登录进程
_login_lock = threading.Lock()
_login_process = None

OK_HTML = '''<!doctype html><html><head>
<meta charset="utf-8"><meta http-equiv="Cache-Control" content="no-store" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>已触发</title>
<style>body{font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;color:#333}</style>
<script>setTimeout(function(){try{window.close();}catch(e){}},150);setTimeout(function(){try{location.replace('about:blank');}catch(e){}},800);</script>
</head><body>已触发恢复，无需停留，可直接返回钉钉。</body></html>'''


def no_store(resp):
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


def is_login_process_running():
    """检查登录进程是否正在运行"""
    global _login_process
    if _login_process is None:
        return False
    if _login_process.poll() is not None:  # 进程已结束
        _login_process = None
        return False
    return True


@app.get('/start-login')
def start_login():
    global _login_process
    token = request.args.get('token', '')
    # TODO: 上线前启用 token 校验
    
    # 使用线程锁防止并发启动
    if not _login_lock.acquire(blocking=False):
        app.logger.warning("登录进程启动请求被拒绝：上一次请求正在进行")
        resp = redirect('/_ok', code=302)
        return no_store(resp)
    
    try:
        # 检查是否已有登录进程在运行
        if is_login_process_running():
            app.logger.info("登录进程已在运行中")
            resp = redirect('/_ok', code=302)
            return no_store(resp)
        
        # 启动登录子进程
        script_path = '/app/login_only.py'  # 容器内路径
        env = os.environ.copy()
        
        app.logger.info(f"启动登录进程: {script_path}")
        _login_process = subprocess.Popen(
            [sys.executable, script_path],
            cwd='/app',
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        app.logger.info(f"登录进程已启动: pid={_login_process.pid}")
        
        # 启动一个后台线程来清理完成的进程
        def cleanup_process():
            global _login_process
            try:
                if _login_process:
                    _login_process.wait()
                    app.logger.info(f"登录进程已完成: pid={_login_process.pid}")
                    _login_process = None
            except Exception as e:
                app.logger.error(f"清理登录进程时出错: {e}")
        
        threading.Thread(target=cleanup_process, daemon=True).start()
        
    except Exception as e:
        app.logger.error(f'启动登录进程失败: {e}')
    finally:
        _login_lock.release()
    
    resp = redirect('/_ok', code=302)
    return no_store(resp)


@app.get('/_ok')
def ok_page():
    resp = make_response(OK_HTML, 200)
    resp.mimetype = 'text/html; charset=utf-8'
    return no_store(resp)


@app.get('/healthz')
def healthz():
    return ('ok', 200)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8999)

