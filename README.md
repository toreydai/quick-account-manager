# quick-account-manager

Amazon Quick（QuickSight）SSO 账号管理后台——把一套原本纯命令行的建号流程（单人建号 +
xlsx 批量导入），包一层带权限控制和审计的 Web 界面。建号只开放作者专业版/作者版两档；
已建号用户的管理范围（档位变更/停用/硬删除，单人 + 批量）覆盖全部 6 档；硬删除要求
先停用、审计日志记原档位；另有审计日志过滤。多账号管理明确不做，见
[`docs/design.md`](docs/design.md)。

完整设计规格、每一个技术决策的取舍理由，都在 `docs/design.md` 里，这份 README 只讲
最短路径怎么跑起来，详细步骤在对应的 `docs/` 文档里。

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
| [`docs/deployment.md`](docs/deployment.md) | 部署与测试手册：Keycloak 前置配置、本地/Docker/AWS 生产部署分步骤指南、测试范围 |
| [`docs/user-guide.md`](docs/user-guide.md) | 面向管理员的操作手册（建号/改档位/停用/删除/审计日志） |

## 本地开发

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # 本地开发可以用假的 client secret，不连真实 Keycloak 的话 /auth/login 会报错但其余页面能正常跑

alembic upgrade head
uvicorn app.main:app --reload
```

访问 `http://localhost:8000/health` 应该返回 `{"status": "ok"}`。要连真实 Keycloak
测登录流程，先按 [`docs/deployment.md` §6](docs/deployment.md#6-keycloak-前置配置人工一次性)
建好两个 client，再把真实 client id/secret 填进 `.env`。

## 测试

```bash
pytest
```

覆盖密码生成、角色-组映射、批量导入校验、审计日志、Keycloak client token 缓存等逻辑，
详细覆盖范围和手工端到端测试清单见 [`docs/deployment.md` §3](docs/deployment.md#3-测试)。

## Docker / 部署到 AWS

```bash
cd deploy
docker compose up --build
```

生产走独立 CloudFormation 栈（`infra/template.yaml`），复用现有 ALB、新建独立 EC2，
不修改任何现有生产资源。完整部署步骤（Keycloak 前置配置、参数说明、已知坑、验证清单）
见 [`docs/deployment.md`](docs/deployment.md)。

## License

MIT - see the [LICENSE](LICENSE) file for details.

## 免责声明

- 本项目仅供学习与技术参考，不构成生产部署方案。
- 运行过程中会调用 Keycloak Admin REST API 和 AWS QuickSight API，请根据实际使用量评估成本。
- 建号/重置密码生成的初始密码属于敏感数据，只在页面上一次性展示、不落库，请管理员妥善转发，不要截图或转发给无关人员。
- 作者不对因使用本项目产生的任何费用、数据泄露或其他损失承担责任。
- 本项目与 Amazon Web Services 无官方关联，相关服务的可用性与定价以 AWS 官方文档为准。
- 生产环境使用前请根据实际需求进行安全评估与调整。
