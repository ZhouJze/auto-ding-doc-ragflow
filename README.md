# 项目说明
因业务需求，需要把钉钉知识库的文档同步到RAGFlow，作为AI助理。

调研发现钉钉知识库没有开放端口实现下载功能，所以有了这个项目。

目前实现原理为手动PRC注入JS的方式，通过无头浏览器实现自动下载。

因为Cookie有时效问题，所以目前还不是全自动，在Cookie过期时，配置的钉钉机器人会发送告警，并提示维护人手动扫码登录，更新Cookie。

所以算是一个半自动化的解决方案。

具体流程图如下：

![钉钉文档同步自动化.jpg](%E9%92%89%E9%92%89%E6%96%87%E6%A1%A3%E5%90%8C%E6%AD%A5%E8%87%AA%E5%8A%A8%E5%8C%96.jpg)![钉钉文档同步流程图](![钉钉文档同步自动化.jpg](%E9%92%89%E9%92%89%E6%96%87%E6%A1%A3%E5%90%8C%E6%AD%A5%E8%87%AA%E5%8A%A8%E5%8C%96.jpg))

---

# Ding Docker 部署指南（Linux）

在 Linux 服务器上部署 `ding-docker` 并实现每日凌晨增量更新。

---

## 1. 前置条件

- 服务器已安装 **Docker CE** 和 **Docker Compose Plug-in**（`docker compose` 命令可用）。
- 服务器网络可访问钉钉、RagFlow、孔明 API、图床等外部服务。
- 拥有项目 Git 仓库的访问权限。

---

## 2. 获取代码

```bash
mkdir -p /opt/ding-doc-downloader
cd /opt/ding-doc-downloader
git clone <your-git-origin> .
```

> 根据实际情况调整路径或仓库地址。

---

## 3. 配置环境变量

1. 切换目录并复制模板：

   ```bash
   cd auto_downloader/ding-docker
   cp .env.example .env
   ```

2. 编辑 `.env`，填写必要字段：

   | 变量 | 说明                                    |
   | ---- |---------------------------------------|
   | `DING_ROBOT_ACCESS_TOKEN`/`DING_ROBOT_SECRET` | 钉钉自定义机器人凭证（必填）                        |
   | `AT_MOBILES`, `AT_USER_IDS` | 需要 @ 的手机号 / userId（逗号分隔，可选）           |
   | `DING_LOGIN_URL` | 钉钉登录页 URL（默认即可）                       |
   | `PICUI_TOKEN` | 图床 token                              |
   | `USER_DATA_DIR`, `HEADLESS`, `DEBUG_SHOTS` 等 | 登录脚本运行参数，如需可见浏览器可设 `HEADLESS=false`   |
   | `TARGET_URLS`/`TARGET_URL` | 钉钉知识库根目录 URL（多个用逗号、分号或换行分隔）           |
   | `KONGMING_BASE` | 孔明 API 基地址（默认指向测试环境，若要正式环境请替换）        |
   | `RAGFLOW_BASE/RAGFLOW_TOKEN/RAGFLOW_DATASET_ID` | RagFlow 接口地址及凭证（必填）                   |
   | `TRIGGER_BASE_URL` | 登录通知中的扫码链接，默认 `http://localhost:8999` |
   | `MIN_TS` | 文档过滤的最小更新时间（秒，Unix 时间戳），留空则不过滤        |

---

## 4. 构建并启动容器

```bash
cd /opt/ding-doc-downloader/auto_downloader/ding-docker
docker compose pull      # 如有镜像更新，可先 pull
docker compose build     # 首次部署或依赖更新时执行
docker compose -p ding_docker up -d     # 后台启动容器
```

容器启动后会运行一个 Flask 服务（监听 8999 端口）。

---

## 5. 首次登录钉钉

1. 在浏览器访问：`http://<服务器IP>:8999/start-login`
2. 按提示扫描二维码完成登录。

成功后，容器会在 `/data/log/login/login_YYYYMMDD.log` 记录登录过程，钉钉机器人也会推送相关提示。

---

## 6. 首次全量更新

登录成功后，建议立即执行一次全量更新：

```bash
docker exec ding-docker python -m PRC.main --mode full
```

这会遍历配置的所有目录，将文档上传至 RagFlow 与孔明。

---

## 7. 设置每日凌晨增量更新

编辑服务器的 crontab：

```bash
crontab -e
```

添加任务（示例：每天 00:05 运行增量更新）：

```
5 0 * * * docker exec ding-docker python -m PRC.main --mode incremental >> /var/log/ding-docker-incremental.log 2>&1
```

说明：
- 使用 `docker exec` 调用容器内脚本。
- 日志重定向至 `/var/log/ding-docker-incremental.log` 便于查看。
- 如服务器已有其它任务，可根据实际情况调整时间。

---

## 8. 常用维护命令

| 操作 | 命令 |
| ---- | ---- |
| 查看容器日志 | `docker compose logs -f ding-docker` |
| 进入容器调试 | `docker exec -it ding-docker bash` |
| 重新登录（登录态失效） | 浏览器访问 `http://<服务器IP>:8999/start-login` |
| 手动增量更新 | `docker exec ding-docker python -m PRC.main --mode incremental` |
| 停止/启动容器 | `docker compose down` / `docker compose up -d` |

---

## 9. 数据持久化位置

| 容器内路径 | 宿主机映射（默认） | 说明 |
| ---------- | ----------------- | ---- |
| `/data/download` | `auto_downloader/ding-docker/data/download` | 下载的原始/PDF 文件 |
| `/data/log` | `auto_downloader/ding-docker/data/log` | 登录、增量更新日志 |
| `/data/export_state.json` | `auto_downloader/ding-docker/data/export_state.json` | 导出状态记录 |
| `/data/id_mapping.json` | 同上 | UUID → RagFlow/孔明映射 |

如需调整挂载路径，可修改 `docker-compose.yml` 中的 `volumes`。

---

## 10. 更新代码

当仓库有新版本时，可执行：

```bash
cd /opt/ding-doc-downloader
git pull
cd auto_downloader/ding-docker
docker compose build        # 依赖有更新时执行
docker compose up -d        # 可加 --force-recreate 重启容器
```

由于源码通过卷挂载，大部分 Python 脚本改动无需重建镜像即可生效；若依赖发生变化或新增 Python 包，仍建议执行 `build` 后再 `up`。

---

至此，首次部署流程完成，系统会在每日凌晨自动跑增量更新。如遇异常，可结合 `docker compose logs` 与 `/data/log/*` 进行排查。
