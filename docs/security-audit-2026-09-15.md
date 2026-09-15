# quick-account-manager 安全扫描报告

扫描日期：2026-09-15  
范围：`quick-account-manager` 应用代码、Docker/CloudFormation 部署配置、当前 ZKTJ 线上部署的关键运行态。  
结论：已部署 CSRF、安全响应头、业务上传限制及容器加固，但 Starlette 风险未清零。复核发现 10 条扫描记录实际对应 5 个不同 CVE，其中普通表单解析拒绝服务漏洞在本应用中可达。之前“最新版 FastAPI 仍不支持 Starlette 1.x，必须等待”的结论错误：限制来自本项目固定的 FastAPI 0.128.8，官方 FastAPI 0.141.1 已允许 Starlette 1.x。详见下方专项复核。

## Starlette 专项复核

2026-09-15 重新执行 `pip-audit -r requirements.txt --format json`：Starlette 0.52.1 的 10 条记录按 CVE 去重后为 5 项。

| CVE | 当前应用判断 | 官方修复版本 |
|---|---|---|
| CVE-2026-54283 | 高优先级：普通表单字段数量/大小限制失效；FastAPI 在路由内登录与 CSRF 检查之前解析 Form 参数，未登录请求也可达 | 1.3.1 |
| CVE-2026-48710 | Host 导致 URL 路径解释不一致；现有 TrustedHost 白名单拦截本次畸形 Host 用例，应用未使用 request.url.path 作权限判断，未证明认证绕过 | 1.0.1 |
| CVE-2026-54282 | 畸形请求路径污染 URL hostname；未发现应用以 request.url 主机名作权限判断，未证明当前部署可利用 | 1.3.0 |
| CVE-2026-48818 | Windows 静态文件 UNC/NTLM 泄露；当前 Linux 容器不满足平台条件 | 1.1.0 |
| CVE-2026-48817 | HTTPEndpoint 非标准方法调用；应用未定义此类端点，路由显式声明 HTTP 方法，不满足已知触发条件 | 1.1.0 |

本地有界验证（不向线上发送攻击流量）：

- 6897 字节、1001 字段的普通表单，在 `max_fields=10` 下仍被全部接受，证实解析限制失效。
- 通过 ASGITransport 发送未登录 `/users/create` 请求，记录到 `Request.form()` 执行，最终因缺少必填字段返回 422；测试将数据库/Keycloak 依赖替换为空实现，未调用真实服务。
- 畸形 Host `testserver/abc?bar=` 返回 400。
- 未做资源耗尽、并发压力或线上利用测试，不能据此量化实际服务中断阈值。

现有 5MB 校验位于上传端点函数内，晚于 FastAPI 自动表单解析，且不覆盖普通表单。CSRF 和安全响应头不修复此解析漏洞。

升级路径：官方 PyPI 的 FastAPI 0.141.1 依赖为 `starlette>=0.46.0`，`pip install --dry-run --ignore-installed fastapi==0.141.1 starlette==1.3.1` 解析成功。此结果仅证明版本依赖可解，尚未验证应用整体升级兼容性，也未部署新版本。应联合升级并完成登录、用户操作和批量导入回归，补充解析前请求体限制与回归用例。

官方来源：[表单 DoS](https://github.com/Kludex/starlette/security/advisories/GHSA-82w8-qh3p-5jfq)、[Host 路径污染](https://github.com/Kludex/starlette/security/advisories/GHSA-86qp-5c8j-p5mr)、[路径污染主机名](https://github.com/Kludex/starlette/security/advisories/GHSA-jp82-jpqv-5vv3)、[Windows UNC](https://github.com/Kludex/starlette/security/advisories/GHSA-wqp7-x3pw-xc5r)、[HTTPEndpoint](https://github.com/Kludex/starlette/security/advisories/GHSA-x746-7m8f-x49c)、[FastAPI 元数据](https://pypi.org/pypi/fastapi/0.141.1/json)。

## 修复状态摘要

| 优先级 | 问题 | 状态 |
|---|---|---|
| P0 | 依赖存在已知漏洞，包含 `authlib`、`starlette`、`python-multipart`、`jinja2` 等 | 已升级部分依赖；Starlette 剩余 5 个不同 CVE，联合升级待验证 |
| P0 | 所有状态变更 POST 缺少 CSRF token | 已修复 |
| P1 | Session cookie 未显式设置 `Secure`，应用缺少安全响应头 | 已修复 |
| P1 | xlsx 上传无大小/行数限制，存在 DoS 风险 | 已修复 |
| P1 | Docker 镜像/容器默认 root，基础镜像未 pin digest | 已加固；基础镜像已固定 tag，后续可进一步 pin digest |
| P1 | CloudFormation 模板未写 IMDSv2/EBS 加密，线上 EBS 当前未加密 | 模板已修复；现有未加密 EBS 需重建/迁移 |
| P2 | 审计日志写失败时 fail-open | 可接受但需告警 |

## 本次修复记录

- 依赖升级：`fastapi 0.128.8`、`authlib 1.6.12`、`python-multipart 0.0.32`、`jinja2 3.1.6`、`uvicorn 0.39.0`、`httpx 0.28.1`、`boto3 1.43.94`、`urllib3 2.7.0`、`click 8.3.3`、`python-dotenv 1.2.2`、`pydantic-settings 2.14.2` 等。
- CSRF：新增 session 级 CSRF token，覆盖所有 9 个状态变更 POST，包括批量建号 SSE POST 和 JS 动态批量表单。
- Cookie/安全头：生产环境 session cookie 默认 `Secure`；新增 `TrustedHostMiddleware`；统一输出 `X-Content-Type-Options`、`X-Frame-Options`、`Referrer-Policy`、`Content-Security-Policy`，HTTPS 请求输出 HSTS。
- 上传限制：xlsx 上传最多 5MB，最多解析 1000 行；异常 `Content-Length` 直接拒绝。
- 容器：基础镜像改为固定 tag `python:3.12.12-slim-bookworm`，应用以非 root 用户运行；compose 增加只读根文件系统、`tmpfs /tmp`、`cap_drop: ALL`、`no-new-privileges`、`pids_limit`。
- CloudFormation：部署桶启用 versioning；EC2 模板显式要求 IMDSv2；EBS 模板显式加密；Docker Compose 下载固定版本并校验 SHA256。
- 回归测试：新增安全响应头测试和 xlsx 行数限制测试。

## 主要发现

### 线上部署复核（2026-09-15）

- 应用版本：`6476c3d`，已推送 GitHub 并部署到 `i-05f125f400c51017b`。
- 完整测试：Python 3.12 环境 `pytest -q`，82 项通过。
- 运行状态：容器 `/health` 正常，ALB target 状态 `healthy`，外部 HTTPS `/health` 返回 200。
- 运行权限：`user=app`、只读根文件系统、移除全部 capabilities、`no-new-privileges`、进程数上限 256。
- 线上容器覆盖 `ALLOWED_HOSTS=quick-admin.geovisearth.com,localhost,127.0.0.1,172.31.94.226`，包含本实例 IP 以允许 ALB 健康检查。
- 线上容器覆盖 `FORWARDED_ALLOW_IPS=127.0.0.1,172.31.0.0/16`，信任本 VPC 代理提供的 HTTPS 转发信息；8000 端口仍仅允许 ALB 安全组访问。重建容器时须保留这两项配置；迁移实例/VPC 时按实际地址调整。使用 Compose 时可将这两项写入线上 `.env`。
- 原容器保留为停止状态，供必要时回退。现有 EBS 加密迁移及剩余 Starlette 漏洞仍未完成，不应将本次上线视为漏洞清零。

以下“主要发现”中的旧版本和代码状态为修复前证据，当前修复状态见上方摘要与修复记录。

### P0-1 依赖漏洞较多

证据：

- `requirements.txt` 固定了较旧版本：`fastapi==0.111.0`、`starlette==0.37.2`、`authlib==1.3.1`、`python-multipart==0.0.9`、`jinja2==3.1.4`。
- `pip-audit -r requirements.txt --no-deps` 发现 10 个声明依赖包共 40 个已知漏洞。

重点包：

| 包 | 当前版本 | 漏洞数 | 建议 |
|---|---:|---:|---|
| `authlib` | `1.3.1` | 10 | 升到 `>=1.6.12`，尤其涉及 OAuth/OIDC state/cache CSRF 与 token 处理 |
| `starlette` | `0.37.2` | 7 | 随 FastAPI 一起升级，避免框架层请求/表单解析漏洞 |
| `python-multipart` | `0.0.9` | 7 | 升到最新可用版本，上传解析 DoS/路径类问题风险较高 |
| `jinja2` | `3.1.4` | 3 | 至少升到 `3.1.6` |
| `urllib3` | `1.26.20` | 5 | 升到兼容的 `2.x`，需验证 boto3/botocore 约束 |

影响：

应用依赖 OIDC 登录、表单上传、模板渲染；这些包正好处在认证和输入解析路径上，应优先升级。

建议修复：

1. 新建升级分支。
2. 升级 FastAPI/Starlette/Authlib/python-multipart/Jinja2/httpx/boto3 及传递依赖。
3. 跑全量单元测试和一次真实 Keycloak 登录/建号/批量上传验证。
4. 重新构建镜像并部署。

### P0-2 状态变更接口缺少 CSRF 防护

证据：

- Session cookie 认证在 `app/main.py:16` 配置。
- 写操作接口均为普通 POST 表单：`app/api/users.py:211`、`:341`、`:379`、`:467`、`:516`、`:599`、`:653`、`:723`、`:779`。
- 表单模板中没有 CSRF hidden token。

影响：

管理员已登录时，如果访问同站点或同 site 范围内的恶意页面，可能被诱导提交创建用户、重置密码、停用、改档位、删除等操作。`SameSite=Lax` 能降低跨站 POST 风险，但不能替代 CSRF token，尤其在同父域子域被攻破、浏览器差异或未来配置变化时风险仍然存在。

建议修复：

- 引入 CSRF token：登录后生成 token 存 session，所有 POST 表单带 hidden 字段，后端统一校验。
- 对批量 SSE POST、动态 JS 生成表单也加入 token。
- 对高危操作继续保留二次确认邮箱，不要因为加 CSRF 就删掉。

### P1-1 Cookie 和安全响应头不足

证据：

- `SessionMiddleware` 只设置了 `same_site="lax"`，未显式设置 `https_only=True`：`app/main.py:16`。
- 线上入口根路径响应头缺少 `Strict-Transport-Security`、`Content-Security-Policy`、`X-Frame-Options`、`X-Content-Type-Options` 等。

影响：

- Cookie 没有 `Secure` 标志时，如果未来 HTTP 入口或错误代理配置出现，session cookie 可能被明文发送。
- 缺少安全头会增加点击劫持、MIME sniffing、XSS 后果扩大等风险。

建议修复：

- `SessionMiddleware(..., https_only=True, same_site="strict" 或 "lax")`。
- 增加中间件统一设置：`Strict-Transport-Security`、`Content-Security-Policy`、`X-Frame-Options: DENY` 或 `SAMEORIGIN`、`X-Content-Type-Options: nosniff`、`Referrer-Policy`。
- 增加 `TrustedHostMiddleware`，只允许 `quick-admin.geovisearth.com` 和本地开发域名。

### P1-2 xlsx 上传无大小/行数限制

证据：

- `app/api/users.py:351` 直接 `await file.read()` 读完整上传内容。
- `app/services/batch_import.py:45` 直接用 `openpyxl.load_workbook()` 解析。
- `parse_xlsx()` 当前没有文件大小、行数、列数、超时限制。

影响：

登录管理员或被 CSRF 利用的管理员会话可以上传超大 xlsx，导致内存/CPU 消耗，拖慢或打挂单实例服务。

建议修复：

- 读取前检查 `Content-Length`，限制例如 2-5 MB。
- `file.read(max_size + 1)` 后拒绝超限。
- 限制最大数据行数，例如 500 或 1000。
- 对异常返回通用错误，避免把解析库内部细节全部暴露给前端。

### P1-3 Docker/供应链硬化不足

证据：

- `deploy/Dockerfile:1` 使用 `python:3.12-slim`，未 pin digest。
- Dockerfile 未创建非 root 用户，容器默认 root 运行。
- `deploy/docker-compose.yml:6` 将 `8000:8000` 绑定到所有接口。
- CloudFormation UserData 下载 Docker Compose 使用 `latest` URL，未校验 checksum：`infra/template.yaml:222-225`。

影响：

镜像或二进制上游漂移会造成不可复现部署；容器逃逸或应用漏洞后的破坏半径更大。

建议修复：

- pin `python:3.12-slim@sha256:...`。
- Dockerfile 创建 `app` 用户并 `USER app`。
- compose 增加 `read_only: true`、`cap_drop: ["ALL"]`、只给 `/app/data` 写权限。
- 如果只通过 ALB 访问，生产 compose 可绑定 `127.0.0.1:8000:8000` 或继续依赖安全组；二者至少要在文档中明确。
- Docker Compose 二进制固定版本和 SHA256。

### P1-4 CloudFormation 模板硬化缺口

证据：

- 模板未显式设置 `MetadataOptions.HttpTokens: required`。
- 模板 EBS 未显式 `Encrypted: true`：`infra/template.yaml:204-209`。
- 当前线上 EC2 实际 IMDSv2 已是 `required`，但当前线上 EBS 卷 `vol-02ca333456b16d736` 查询为 `Encrypted=false`。
- S3 bucket 已开启 Public Access Block 和 AES256 加密，但未启用 versioning。

影响：

模板重建时可能丢失 IMDSv2 加固；未加密 EBS 在快照/备份/磁盘处置场景下风险更高。

建议修复：

- 在模板 EC2 上加入：
  - `MetadataOptions: { HttpTokens: required, HttpEndpoint: enabled }`
  - `BlockDeviceMappings[].Ebs.Encrypted: true`
- S3 部署桶开启 versioning/lifecycle，方便回滚和自动清理旧包。
- 评估当前未加密 EBS：用加密快照重建卷或重建实例。

### P2-1 审计日志写入失败时 fail-open

证据：

- `app/services/audit_service.py` 捕获所有异常并 `return None`，业务操作继续成功。

影响：

这是为了避免 Keycloak 操作已生效后因为审计库失败导致用户看到错误，设计上可以理解；但如果磁盘满、SQLite 锁死或 DB 迁移失败，关键操作会缺审计记录。

建议修复：

- 保持业务 fail-open 也可以，但要对审计写失败做告警。
- `/health` 可增加可选深度检查或单独 `/ready`，覆盖 DB 可写性。
- CloudWatch 收集应用日志，匹配“审计日志写入失败”告警。

## 已有安全控制

- 未发现硬编码生产密码；生产 secret 通过环境变量/SSM 传入，代码中有弱 secret 启动校验。
- Account Manager 登录依赖 Keycloak OIDC，不维护本地密码表。
- 管理后台授权 fail-closed：没有 `groups` claim 或不在 `/quick-admin-pro` 会拒绝。
- 删除、停用、改档位均会后端反查 `user_id`，不信任隐藏表单里的邮箱。
- 删除账号要求先停用，并要求手动输入确认邮箱。
- 最后一个 `/quick-admin-pro` 管理员保护已实现。
- 创建用户失败时有回滚逻辑，避免孤儿账号。
- ALB 到 EC2 入站安全组只允许来自现有 ALB SG 的 `8000`。
- 线上 ALB 已启用 access logs、drop invalid headers、desync mitigation defensive、deletion protection。

## 扫描命令与结果

```bash
.venv/bin/bandit -r app -ll -ii
```

结果：无中高危发现。Bandit 仅报告 1 个低危误报：`AuditAction.RESET_PASSWORD = "reset_password"` 被识别为疑似硬编码密码。

```bash
.venv/bin/pip-audit -r requirements.txt --no-deps --cache-dir /tmp/pip-audit-cache
```

修复前结果：10 个声明依赖包共 40 个已知漏洞。

修复后使用线上同版本 Python 3.12 环境复扫：

```bash
/tmp/qam-py312-audit/bin/pip-audit -r requirements.txt --no-deps --cache-dir /tmp/pip-audit-cache
```

结果：`starlette 0.52.1` 相关 10 条记录，后续复核去重为 5 个 CVE。项目固定的 `fastapi==0.128.8` 要求 `starlette<1.0.0,>=0.40.0`；官方较新版本已允许 Starlette 1.x，可联合升级。CSRF、业务上传限制、安全响应头不能代替表单解析漏洞修复，详见上方专项复核。

```bash
aws cloudformation validate-template --template-body file://infra/template.yaml
```

结果：模板语法校验通过。

```bash
timeout 60 .venv/bin/pytest tests/test_config_secrets.py tests/test_password_generation.py tests/test_role_mapping.py tests/test_create_user_flow.py tests/test_delete_user.py tests/test_last_admin_guard.py tests/test_partial_failure_recovery.py -q
```

修复前结果：36 passed。

修复后使用 Python 3.12 跑核心套件：

```bash
timeout 60 /tmp/qam-py312-audit/bin/pytest tests/test_health.py tests/test_batch_import.py tests/test_config_secrets.py tests/test_password_generation.py tests/test_role_mapping.py tests/test_create_user_flow.py tests/test_delete_user.py tests/test_last_admin_guard.py tests/test_partial_failure_recovery.py -q
```

结果：52 passed。

原同步 `TestClient` 在新版 Starlette 测试栈里会卡住，`tests/test_health.py` 已改成 `httpx.ASGITransport` 异步测试；应用 ASGI 路径验证通过。

## 建议修复顺序

1. 先升级依赖，尤其是 `authlib`、`starlette/FastAPI`、`python-multipart`、`jinja2`。
2. 加 CSRF token，并覆盖所有 POST 路由和批量建号 SSE POST。
3. 设置 Secure cookie、TrustedHostMiddleware、安全响应头。
4. 给上传加大小/行数限制。
5. 加固 Dockerfile 和 compose。
6. 修 CloudFormation 模板：IMDSv2、EBS 加密、S3 versioning/lifecycle、Docker Compose 固定版本校验。
7. 加审计日志失败告警和 DB 可写性健康检查。
