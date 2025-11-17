# 基础镜像：与 downloader 对齐，包含 Python 及 Playwright 运行依赖
FROM mcr.microsoft.com/playwright/python:v1.46.0-jammy

WORKDIR /app

# ========================= 可配置环境变量（含默认值） =========================
# 非敏感默认参数，可按需在运行时 -e 覆盖；敏感参数默认留空，运行时注入

# 基础行为
# - USER_DATA_DIR：Playwright 持久化上下文目录（需与宿主卷对应）
# - HEADLESS：是否无头运行（true/false），默认无头；有头建议配合 xvfb（见 entrypoint）
# - DEBUG_SHOTS：调试开关。true 时发送每一步截图；false 仅发送关键截图/文本
# - CLEAR_USER_DATA：启动前是否清空 USER_DATA_DIR 内容（强制每次登录）
# - TRY_DESKTOP_TIMEOUT_S：扫码后等待跳到桌面页的超时时间（秒）
ENV USER_DATA_DIR=/app/persistent_context/Default \
    HEADLESS=true \
    DEBUG_SHOTS=false \
    CLEAR_USER_DATA=true \
    TRY_DESKTOP_TIMEOUT_S=90 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    DEBIAN_FRONTEND=noninteractive

# 业务地址
# - DING_LOGIN_URL：钉钉登录页（默认官方入口）
# - PICUI_API：图床 API 根路径（需包含 /api/v1），例如：https://api.picui.cn/api/v1
ENV DING_LOGIN_URL="https://login.dingtalk.com/oauth2/challenge.htm?redirect_uri=https%3A%2F%2Falidocs.dingtalk.com%2Fi%2F%3Fspm%3Da2q1e.24441682.0.0.2c8b252137UE4J&response_type=none&client_id=dingoaxhhpeq7is6j1sapz&scope=openid" \
    PICUI_API="https://picui.cn/api/v1"

# 凭证（敏感信息，默认留空；请在运行时通过 -e 或 orchestrator 注入）
# - DING_ROBOT_ACCESS_TOKEN：钉钉自定义机器人 access_token
# - DING_ROBOT_SECRET：钉钉自定义机器人签名 secret
# - AT_MOBILES：逗号分隔的手机号列表，用于 @ 指定人（如有需要）
# - PICUI_TOKEN：PicUI 私有图床 token（如使用）
# - SMMS_TOKEN：sm.ms 图床 token（可选）
ENV DING_ROBOT_ACCESS_TOKEN="" \
    DING_ROBOT_SECRET="" \
    AT_MOBILES="" \
    PICUI_TOKEN="" \
    SMMS_TOKEN=""

# 安装系统依赖（含时区设置为亚洲/上海）
RUN apt-get update && apt-get install -y \
    xvfb \
    curl \
    tzdata \
    && ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone \
    && dpkg-reconfigure -f noninteractive tzdata \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
RUN pip install --no-cache-dir \
    --index-url https://mirrors.aliyun.com/pypi/simple \
    playwright==1.55.0 \
    requests==2.32.5 \
    flask==3.0.0 \
    pydantic==2.9.2

# 复制所有脚本
COPY app.py /app/app.py
COPY login_only.py /app/login_only.py
COPY PRC /app/PRC

# 确保 Playwright 浏览器已安装
RUN playwright install chromium

# 暴露端口
EXPOSE 8999

# 主进程：启动 Flask HTTP 服务
CMD ["python", "app.py"]

