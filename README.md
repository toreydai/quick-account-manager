# quick-account-manager

Amazon Quick（QuickSight）SSO 账号管理后台——把 [`xuechuan-quick-sso`](../xuechuan-quick-sso) 里 `add_single_user.py`/`create_users_from_xlsx.py` 那套命令行建号流程，包一层带权限控制和审计的 Web 界面。建号（单人 + xlsx 批量导入，带模板下载、批量建号带实时进度）只开放作者专业版/作者版两档；已建号用户的管理范围（档位变更/停用/硬删除，单人 + 批量改档位/批量停用启用）覆盖全部 6 档；硬删除要求先停用、审计日志记原档位；另有审计日志过滤。多账号管理明确不做，见 [`docs/design.md`](docs/design.md)。

完整设计规格、每一个技术决策的取舍理由，都在 `docs/design.md` 里，这份 README 只讲怎么跑起来。

## 技术栈

FastAPI + Jinja2 服务端渲染（不用 React/Vite——3 个管理员用的内部工具没必要背一套前端构建链）+ SQLAlchemy + Alembic + SQLite。登录走 Keycloak OIDC，不维护独立账号密码表。

```
app/
├── api/          路由：auth（登录）、users（建号/批量建号/重置密码/档位变更/停用/列表）、audit、health
├── services/     keycloak_client（token 管理）、keycloak_service（业务逻辑）、batch_import（xlsx 解析校验）、quicksight_service（订阅状态只读核对）、audit_service
├── models/       audit_log（唯一需要持久化的表）
├── templates/    Jinja2
├── static/       CSS + 一小段原生 JS（表格搜索，没有构建步骤）
└── core/         config / db / security / deps
alembic/          数据库 migration
tests/            pytest，覆盖密码生成规则、角色映射、审计日志、Keycloak client token 缓存逻辑
deploy/           Dockerfile + docker-compose.yml
infra/            CloudFormation 模板
```

## 文档

| 文档 | 内容 |
|---|---|
| [`docs/design.md`](docs/design.md) | 完整设计决策记录，每一次范围变更的时间线和取舍理由 |
| [`docs/architecture.md`](docs/architecture.md) | 系统架构：部署拓扑、内部分层、关键数据流、安全边界 |
| [`docs/deployment.md`](docs/deployment.md) | 部署与测试手册：本地/Docker/AWS 生产部署分步骤指南、单元测试范围、手工端到端测试清单 |
| [`docs/user-guide.md`](docs/user-guide.md) | 面向管理员的操作手册（建号/改档位/停用/删除/审计日志） |

这份 README 只讲最短路径怎么跑起来，详细步骤和故障排查在对应的 `docs/` 文档里。

## 部署前置依赖（人工步骤，代码假设这些已经配好）

在生产 Keycloak（`https://sso.example.com`）的 `quick` realm 里手动建两个 client（这一步本项目的代码不会自动做，也不应该自动做——见 `docs/design.md` 4.1/4.4 节为什么职责要分开）：

1. **Service account client**（4.1 节，后端调 Admin REST API 用）
   - Client ID：跟 `.env` 的 `KEYCLOAK_SERVICE_CLIENT_ID` 一致
   - `Client authentication`: On（confidential），开启 `Service accounts roles`
   - 在 `Service account roles` 里给这个 client 分配 `realm-management` 的 client role：`manage-users`、`query-groups`、`view-users`（不要给 `manage-realm`）

2. **OIDC 登录 client**（4.4 节，管理后台自己的登录用）
   - Client ID：跟 `.env` 的 `KEYCLOAK_OIDC_CLIENT_ID` 一致
   - `Client authentication`: On，`Standard flow`: On（Authorization Code）
   - Valid redirect URI 填 `.env` 的 `OIDC_REDIRECT_URI`
   - **必须加一个 Group Membership mapper**（Client scopes → 对应 scope → Add mapper → By configuration → Group Membership），Token Claim Name 填 `groups`，`Full group path`: On——不加这个，登录后 token/userinfo 里不会带 `groups` claim，应用会拒绝所有人登录（`app/api/auth.py` 里这一步是 fail-closed 设计，宁可全部拒绝也不假装权限检查通过了）
   - 可选：按 `docs/design.md` 4.4 节「登录页视觉区分」给这个 client 配 `login_theme` 属性，跟 `awssso` 的员工 SSO 登录页区分开；这一步在当前部署的 Keycloak 26.6.3 上具体怎么设置还没实测过，留作实施阶段的验证项

这两个 client 的 secret 通过 SSM Parameter Store（SecureString）传给容器，不要写进代码仓库。

3. **（可选）打开 User Profile 的 Unmanaged Attributes**（Realm settings → User profile → Unmanaged attributes → Enabled）——建号时的"中文姓名"会写进 Keycloak 用户的 `attributes.chineseName`，但 Keycloak 26 默认只允许写入 User Profile 里显式声明过的属性，没声明的自定义属性会被**静默丢弃**（不报错，POST 照样 200，属性就是没了）。这一步是本地端到端测试对着真实 Keycloak 26.6.3 验证时才发现的，之前只 mock 测试是测不出来的。不打开这个开关不影响其他任何功能——审计日志里的中文姓名不依赖这个设置，一直会正确记录。

## 本地开发

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # 本地开发可以用假的 client secret，不连真实 Keycloak 的话 /auth/login 会报错但其余页面能正常跑

alembic upgrade head
uvicorn app.main:app --reload
```

访问 `http://localhost:8000/health` 应该返回 `{"status": "ok"}`；`/` 会跳转到 `/auth/login`（本地没配真实 Keycloak 的话点登录会报错，这是预期的，要连真实环境测登录流程需要在 `.env` 里填真实的 client id/secret）。

## 测试

```bash
pytest
```

覆盖范围：密码生成规则（长度/字符类别/避开公式触发字符/不等于用户名邮箱）、角色-组映射（全部 6 档）、批量导入 xlsx 解析校验（必需列缺失/角色非法/文件内 username-邮箱重复/空行跳过/username 列可选）、审计日志读写与过滤（操作人/目标邮箱模糊匹配、操作类型、成功/失败）、Keycloak client 的 token 缓存/401 重试/header 保留逻辑、部分失败时的回滚/重试（建号加组失败自动回滚、改档位删旧组失败重试一次）、硬删除（`DELETE /users/{id}` 调用序列 + 最后一个管理员保护）、生产密钥强度校验、`/health` 端点。这些都是 mock 掉 Keycloak HTTP 层的单元测试，不连真实服务器，**不在 CI/仓库里自动跑**。

建号/重置密码/改档位/停用启用/删除/登录这几个接口额外对着本地起的真实 Keycloak 26.6.3 容器做过手工端到端验证（含故意删掉 Keycloak 里的组来触发真实的加组失败/找不到组场景，确认不会留孤儿账号、不会 500、审计日志如实记录），过程和结论见对应的 git commit message，不是长期自动化测试的一部分——以后再改这几块逻辑，建议重新起一次本地 Keycloak 照着验证一遍，别只信 mock 测试就合并。批量建号的三个路由（上传→预览→确认，确认这一步现在是 SSE 流式响应）、批量改档位/批量停用启用两个路由、硬删除路由，都用 mock 掉 Keycloak 的 `TestClient`（配合伪造的 Keycloak OIDC 会话 cookie 和内存 SQLite）走过一遍完整 HTTP 链路手工验证过（建号成功/跳过/失败三种场景，批量操作里选中自己被跳过，删除邮箱确认不匹配被拒绝，SSE 事件流格式正确），不是自动化测试的一部分，同样建议上线前对着本地真实 Keycloak 容器再跑一遍。

## Docker

```bash
cd deploy
docker compose up --build
```

`docker-compose.yml` 读同目录上一级的 `.env`。容器启动时会先跑 `alembic upgrade head` 再起 `uvicorn`（`deploy/Dockerfile` 里的 `CMD`），SQLite 文件放在具名 volume `qam-data` 里，重建容器不丢数据。

## 部署到 AWS（infra/template.yaml）

CloudFormation 模板已经写好并通过 `aws cloudformation validate-template` 语法校验，**但没有实际部署过**——按 `docs/design.md` 3.1 节的设计，这是一个完全独立的新栈，不修改、不依赖 `../xuechuan-quick-sso/template.yaml` 那个现有生产栈。

```bash
export AWS_PROFILE=xc

# 部署前先照 design.md 3.1 节的运维检查清单核对一遍，确认现有 ALB/Listener 没变：
aws elbv2 describe-load-balancers --region us-east-1 \
  --query 'LoadBalancers[0].{ARN:LoadBalancerArn,DNS:DNSName}'

aws cloudformation deploy \
  --template-file infra/template.yaml \
  --stack-name quick-account-manager \
  --capabilities CAPABILITY_IAM \
  --region us-east-1 \
  --parameter-overrides AlarmNotificationEmail=your-email@example.com
```

部署完成后，栈会新建：一台独立 `t4g.medium` EC2（不开 SSH，走 SSM Session Manager 运维）、一个新 TargetGroup + 挂在现有 ALB `HttpsListener` 上的 host-header ListenerRule（`quick-admin.example.com`）、CloudWatch Alarm + SNS Topic。

**部署后还需要人工做的事**（模板不会自动做，也不该自动做）：

1. 在阿里云 HiChina 加一条 CNAME：`quick-admin.example.com` → 现有 ALB DNS 名（`Outputs.Step1DNS`）
2. 按上面「部署前置依赖」在生产 Keycloak 建两个 client
3. 把 client secret 等敏感配置写进 SSM Parameter Store（SecureString，路径 `/quick-account-manager/*`，实例 IAM Role 已经有对应的只读权限）
4. 代码分发：走 S3 中转，不依赖 GitHub——实例 IAM Role 只拿到这个 S3 bucket 的只读权限，不管代码仓库托管在哪、公开与否都不影响部署流程。`Outputs.AppCodeBucketName` 是专门建的私有 bucket，本地 `git archive --format=tar.gz -o release.tar.gz HEAD` 打包（只含 git 已提交的文件，不含 `.env`/`.venv`/`data/` 这些）→ `aws s3 cp` 上传 → 实例上用自己的 IAM Role（已经有这个 bucket 的只读权限）`aws s3 cp` 拉下来解压到 `/opt/quick-account-manager`
5. 起容器，走 SSM Session Manager（不是真 SSH）在实例上执行

`docs/design.md` 5 节提到的 AWS Backup Plan（EBS 快照）目前**没有**写进这个模板——那一条已经明确标注为"上线后再补，不阻塞 MVP"，不是遗漏。

**预期会看到的告警，不是故障**：`UserData` 只装 Docker，不会自动拉代码 / 起容器，栈刚建完到手动部署完成这段时间 `/health` 连不通，`UnhealthyTargetAlarm` 会正常触发一次 SNS 通知，完成部署后自动恢复。

**首次生产部署踩到的真坑，已确认解法**：AL2023 `dnf` 仓库里没有 `docker-buildx-plugin`，装不上，而 `docker compose up --build`（Compose v5.5.0）会硬性要求 buildx 存在，报错 `compose build requires buildx 0.17.0 or later`，直接卡住起不来容器。**解法是绕开 `docker compose`，改用经典 `docker build` + `docker run`**（效果等价，读同一个 `.env`、同一个 `Dockerfile`）：

```bash
cd /opt/quick-account-manager
sudo docker build -t quick-account-manager:latest -f deploy/Dockerfile .
sudo docker volume create qam-data
sudo docker run -d --name qam-app --restart unless-stopped \
  -p 8000:8000 --env-file .env -v qam-data:/app/data \
  quick-account-manager:latest
```

`deploy/docker-compose.yml` 保留着（本地开发用，本地环境一般有更新的 Docker Desktop/buildx，不会踩这个坑），只是生产这台 AL2023 实例上目前用不了 `docker compose --build` 这条路径。以后想在生产也走 compose，需要单独装 buildx（AL2023 上没有现成 dnf 包，得从 [docker/buildx releases](https://github.com/docker/buildx/releases) 下二进制放到 CLI 插件目录），这次没做，不阻塞上线。

## License

MIT - see the [LICENSE](LICENSE) file for details.

## 免责声明

- 本项目仅供学习与技术参考，不构成生产部署方案。
- 运行过程中会调用 Keycloak Admin REST API 和 AWS QuickSight API，请根据实际使用量评估成本。
- 建号/重置密码生成的初始密码属于敏感数据，只在页面上一次性展示、不落库，请管理员妥善转发，不要截图或转发给无关人员。
- 作者不对因使用本项目产生的任何费用、数据泄露或其他损失承担责任。
- 本项目与 Amazon Web Services 无官方关联，相关服务的可用性与定价以 AWS 官方文档为准。
- 生产环境使用前请根据实际需求进行安全评估与调整。
