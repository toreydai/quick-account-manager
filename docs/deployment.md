# quick-account-manager 部署与测试手册

本文档是分步骤的操作指南，覆盖"改完代码怎么验证"到"怎么部署到生产"的完整流程。
架构层面的取舍理由见 [`architecture.md`](architecture.md) / [`design.md`](design.md)；
这里只讲"照着做能跑起来、跑之前先验证过"。

## 0. 部署形态一览

| 场景 | 用途 | 章节 |
|---|---|---|
| 本地开发（不装 Docker） | 改代码、跑测试 | §1 |
| 本地 Docker | 验证容器化行为，跟生产一致的运行方式 | §2 |
| 测试 | 单元测试 + 手工端到端测试 | §3 |
| AWS 生产（CloudFormation + EC2 + Docker） | 实际给 3 个管理员用 | §5-§8 |

## 1. 本地开发

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # 本地用假 client secret 即可，见 §9 环境变量清单

alembic upgrade head
uvicorn app.main:app --reload
```

验证：`http://localhost:8000/health` 返回 `{"status": "ok"}`；访问 `/` 会跳转
`/auth/login`，本地没配真实 Keycloak 的话点登录会报错，这是预期行为——要连真实环境
测登录流程，需要按 §6 配好两个 Keycloak client 并在 `.env` 里填真实值。

## 2. 本地 Docker

```bash
cd deploy
docker compose up --build
```

`docker-compose.yml` 读同目录上一级的 `.env`。容器启动流程：`alembic upgrade head` →
`uvicorn app.main:app --host 0.0.0.0 --port 8000`（`deploy/Dockerfile` 的 `CMD`）。
SQLite 文件放在具名 volume `qam-data` 里，重建容器不丢数据。

**注意**：生产 EC2（AL2023）上 `docker compose --build` 因为缺 `buildx` 插件跑不通，
见 §8.5 的坑与解法；本地环境一般 Docker Desktop 自带 buildx，不受影响。

## 3. 测试

本项目的测试分三层，覆盖范围和"要不要重新跑"的判断标准不一样，分开说明。

| 层 | 工具 | 是否 mock Keycloak | 是否自动化/进 CI |
|---|---|---|---|
| ① 单元测试 | `pytest` | 是，mock 掉 HTTP 层 | 自动化，但**不在 CI/仓库里自动跑**（本仓库没有 GitHub remote，没有 CI） |
| ② 手工端到端测试 | 本地真实 Keycloak 26.6.3 容器 + 浏览器 | 否 | 手工，不是长期自动化测试的一部分 |
| ③ 手工 HTTP 集成测试 | `TestClient` + mock 会话/内存 SQLite | 是 | 手工跑过一遍，不是自动化测试的一部分 |

**结论先说在前面**：①能防回归，但 Keycloak 权限粒度（例如 `manage-users` 打不动
`partialImport` 这类 403）只有对着真实服务器才会暴露，纯 mock 测试测不出来——这是
本项目开发过程中真实踩过的坑（见 [`design.md` 4.2 节](design.md#42-建号逻辑)）。以后
改动 Keycloak 相关逻辑（`keycloak_service.py`/`keycloak_client.py`），**建议按 §3.2
重新起一次本地真实 Keycloak 照着验证一遍，不要只信①的 mock 测试就合并**——这也是
`test-before-push`（[[feedback_test_before_push]]）原则在这个项目里的具体落地：语法/
mock 测试通过不等于测过。

### 3.1 单元测试（自动化，mock Keycloak）

```bash
pytest
```

16 个测试文件，覆盖：

| 测试文件 | 覆盖内容 |
|---|---|
| `test_password_generation.py` | 密码生成规则：长度、字符类别齐全、避开 Excel/WPS 公式触发字符（`=+-@` 开头）、不等于用户名/邮箱 |
| `test_role_mapping.py` | 角色-组映射，全部 6 档（`ROLE_TO_GROUP`） |
| `test_create_user_flow.py` | 建号两步调用（建号+加组）、加组失败自动回滚删除、回滚也失败时的 `PartialFailureError` |
| `test_batch_import.py` | xlsx 解析校验：必需列缺失、角色非法、文件内 username/邮箱重复、空行跳过、username 列可选 |
| `test_export_service.py` | 用户清单导出 xlsx 的内容/格式 |
| `test_audit_service.py` | 审计日志读写与过滤（操作人/目标邮箱模糊匹配、操作类型、成功/失败） |
| `test_keycloak_client.py` | token 缓存、401 自动重试、请求 header 保留逻辑 |
| `test_delete_user.py` | 硬删除的调用序列（`DELETE /users/{id}`）+ 最后一个管理员保护 |
| `test_last_admin_guard.py` | `LastAdminGuardError`：任何会把 `/quick-admin-pro` 清零的操作都要被拒绝 |
| `test_partial_failure_recovery.py` | 部分失败时的回滚/重试：建号加组失败自动回滚、改档位删旧组失败重试一次 |
| `test_list_users.py` | 用户列表拉取与展示逻辑 |
| `test_user_list_quicksight_concurrency.py` | 订阅状态并发查询逻辑 |
| `test_config_secrets.py` | 生产环境弱密钥/占位符校验（`app/core/config.py` 启动期拒绝） |
| `test_health.py` | `/health` 端点 |

运行单个文件：`pytest tests/test_last_admin_guard.py -v`。

### 3.2 手工端到端测试（真实 Keycloak，未自动化）

以下接口需要对着真实 Keycloak 服务器验证过，因为涉及真实权限粒度/真实组状态，纯 mock
测不出来：**建号、重置密码、改档位、停用/启用、硬删除、登录**。

**准备一个本地 Keycloak：**

```bash
docker run -d --name kc-test -p 8080:8080 \
  -e KEYCLOAK_ADMIN=admin -e KEYCLOAK_ADMIN_PASSWORD=admin \
  quay.io/keycloak/keycloak:26.6.3 start-dev
```

在这个本地 Keycloak 上按 §6 建 `quick` realm、两个 client、`/quick-admin-pro` 组，
以及全部 6 个角色对应的组（`/quick-admin-pro`、`/quick-author-pro`、
`/quick-reader-pro`、`/quick-admin`、`/quick-author`、`/quick-reader`）。`.env` 里
`KEYCLOAK_BASE_URL` 指到 `http://localhost:8080`。

**正常路径清单：**

- [ ] 建单个账号（每个 `CREATE_ALLOWED_ROLES` 档位各建一次）：Keycloak 里能看到新用户
      + 用户在正确的组里，页面上能看到一次性密码
- [ ] 重置密码：Keycloak 用户的密码确实被更新（用新密码能登录 Keycloak 本身验证）
- [ ] 改档位（6 档之间任意切换）：旧组被移除、新组被加入，用户任意时刻都至少在一个
      角色组里（不会出现中间态"不在任何组"）
- [ ] 停用账号：`enabled: false` 后，用这个账号走 Keycloak 登录会被拒绝；重新启用后
      能正常登录
- [ ] 硬删除：先停用再删除的正常顺序，删除后 Keycloak 里查不到这个用户
- [ ] 用 `/quick-admin-pro` 组里的账号登录管理后台，能看到用户列表
- [ ] 用不在该组的账号登录，被 `/auth/forbidden` 拒绝

**故意触发失败场景（防呆护栏验证）：**

- [ ] 建号中途手动删掉目标组，验证加组失败时账号被自动回滚删除（Keycloak 里查不到
      孤儿账号），不是留下一个"建了号但没加组"的半成品
- [ ] 把 `/quick-admin-pro` 组减到只剩 1 人，尝试把这最后一人降级/停用/删除，验证
      `LastAdminGuardError` 生效，操作被拒绝
- [ ] 用管理员账号登录后，尝试对自己执行改档位/停用/删除，验证 `SelfLockoutError`
      生效
- [ ] 对一个 `enabled: true` 的账号直接尝试硬删除（不先停用），验证
      `MustDisableFirstError` 生效
- [ ] 硬删除表单里故意输错确认邮箱，验证请求被拒绝、账号未被删除
- [ ] 不给 OIDC client 配 Group Membership mapper，验证登录后所有人（包括真正的管理员）
      都被拒绝（fail-closed 行为）

以上场景的验证结论以对应的 git commit message 为准，不是长期自动化测试的一部分——
逻辑改动之后建议重新跑一遍，不要假设结论会一直成立。

### 3.3 手工 HTTP 集成测试（TestClient + mock，未自动化）

批量建号（上传 → 预览 → 确认，确认这一步是 SSE 流式响应）、批量改档位/批量停用启用
两个路由、硬删除路由，用 mock 掉 Keycloak 的 `TestClient`（配合伪造的 Keycloak OIDC
会话 cookie 和内存 SQLite）走过一遍完整 HTTP 链路手工验证：

- [ ] 批量建号：成功/跳过（用户名已存在）/失败三种场景都出现在 SSE 事件流里，格式
      正确（`data: {...}\n\n`，结尾有 `event: done`）
- [ ] 批量改档位/批量停用启用：选中操作者自己的那一行被跳过（`SelfLockoutError`），
      不影响其它行继续执行；某一行失败不中断整个循环
- [ ] 硬删除路由：确认邮箱不匹配时被拒绝

同样建议在上线前对着本地真实 Keycloak 容器（§3.2 的环境）再跑一遍，不要只信 mock。

### 3.4 已知未覆盖项

- 没有针对"Keycloak Admin REST API 长时间不可用"的自动化测试（超时/重试策略目前靠
  `keycloak_client.py` 的实现本身，没有单独的故障注入测试）
- 没有负载测试；当前用户规模（几十人）下没有必要，见
  [`architecture.md` §4.3](architecture.md#43-用户列表--订阅状态异步加载)关于并发数
  调优的说明——那次调优本身是靠生产真实反馈（"非常慢"）+ 实测数据驱动的，不是靠压测
- 没有 CI：本仓库不在 GitHub 上，没有接入任何 CI 系统，所有测试都是本地手动触发

## 4. 部署前检查清单

推送/部署前，按下面顺序过一遍：

1. `pytest` 全绿（§3.1）
2. 如果这次改动涉及 `keycloak_service.py`/`keycloak_client.py`/`app/api/auth.py`：
   按 §3.2 对着本地真实 Keycloak 至少跑一遍相关的正常路径 + 故意失败场景
3. 如果这次改动涉及批量操作路由：按 §3.3 用 `TestClient` 跑一遍
4. 本地 `docker compose up --build` 起容器验证一遍（§2），确认镜像能正常构建、
   `/health` 正常
5. 只有以上都过了，才走 §5 起的生产部署流程——**语法校验（比如
   `aws cloudformation validate-template`）不算测过**，`infra/template.yaml`
   过去就出现过语法校验通过但运行时报错的情况（`GroupDescription` 只支持 ASCII 的坑，
   见 §8.6）

## 5. AWS 生产部署总览

```
①核对现有 ALB/Listener ARN 没变（§7.1）
        ↓
②部署 CloudFormation 栈（新建独立 EC2 + TargetGroup + ListenerRule + 告警）（§7.2）
        ↓
③人工：Keycloak 建两个 client（§6）
        ↓
④人工：加 CNAME（§8.1）
        ↓
⑤人工：把 client secret 等敏感配置写进 SSM Parameter Store（§8.2）
        ↓
⑥代码分发到 EC2 + 起容器（§8.3-8.4）
        ↓
⑦验证（§10）
```

②③④⑤⑥都是一次性的人工步骤，模板不会（也不该）自动做——见 `infra/template.yaml`
`UserData` 注释：不在 `UserData` 里写死仓库地址/密钥，避免重蹈此前踩过的坑
（`UserData` 写死某个 fork 仓库地址，仓库改名/删除后 `UserData` 里的地址直接 404）。

## 6. Keycloak 前置配置（人工一次性）

在生产 Keycloak（`https://sso.example.com`）的 `quick` realm 里手动建两个
client。**这一步代码不会自动做**——见 [`design.md` 4.1/4.4 节](design.md)为什么这两个
client 的创建要保持人工/职责分离。

### 6.1 Service account client（后端调 Admin REST API 用）

1. Keycloak Admin Console → `quick` realm → Clients → Create client
2. Client ID 填 `.env` 里 `KEYCLOAK_SERVICE_CLIENT_ID` 的值（默认
   `quick-account-manager-service`）
3. `Client authentication`: **On**（confidential），`Service accounts roles`: **On**
4. 保存后进 `Service accounts roles` 标签页，Assign role → 筛选 `realm-management` →
   勾选 `manage-users`、`query-users`、`query-groups`、`view-users` 四个 client role
   （**不要**勾 `manage-realm` 等更大权限）
5. 确认这个 client 的 `Full Scope Allowed` 是 **On**，或者用等价 client scope 配置
   确保上面四个 `realm-management` roles 会进入 `client_credentials` 换到的 token。
   如果这里没配对，用户列表会报 `Keycloak API error 403: {"error":"HTTP 403 Forbidden"}`。
6. Credentials 标签页复制 client secret，写进 §8.2 的 SSM 参数

### 6.2 OIDC 登录 client（管理后台自己登录用）

1. Clients → Create client，Client ID 填 `.env` 里 `KEYCLOAK_OIDC_CLIENT_ID` 的值
   （默认 `quick-account-manager-web`）
2. `Client authentication`: **On**，`Standard flow`: **On**（Authorization Code），
   其余流程关掉
3. Valid redirect URI 填 `.env` 的 `OIDC_REDIRECT_URI`
   （生产环境是 `https://quick-admin.example.com/auth/callback`）
4. **必须加 Group Membership mapper**（否则登录后所有人都会被拒绝，见下方警告框）：
   Client scopes → 对应 scope（一般是 `dedicated` scope）→ Add mapper →
   By configuration → **Group Membership**
   - Name：任意，比如 `groups`
   - Token Claim Name：`groups`
   - `Full group path`：**On**
5. Credentials 标签页复制 client secret，写进 §8.2 的 SSM 参数
6. 可选：按 [`design.md` 4.4 节](design.md#44-应用自身登录复用-keycloak不单独建账号体系)
   给这个 client 配 `login_theme` 属性，跟 `awssso` 员工 SSO 登录页做视觉区分——这一步
   在 Keycloak 26.6.3 上具体怎么设置未实测过，跳过不影响功能，只是登录页视觉上不好区分
   两个系统

> **⚠️ 第 4 步不能漏**：`app/api/auth.py` 是 fail-closed 设计——登录 token/userinfo
> 里没有 `groups` claim，所有人（包括真正的管理员）都会被拒绝登录（跳到
> `/auth/forbidden`），而不是"假装权限检查通过"。如果部署完发现谁都登不进去，先查
> 这一步有没有漏配。

### 6.3 谁能登录

登录权限完全由 Keycloak 的 `/quick-admin-pro` 组成员关系决定（`.env` 的
`ADMIN_GROUP_PATH`），不需要在应用里额外 bootstrap 一个初始管理员——这个组已经有
真实账号存在，应用一上线就能登录。要加/删管理员，直接去 Keycloak 改这个组的成员，
不用改本应用的任何配置。

### 6.4 （可选）Unmanaged Attributes

如果要让建号时填的"中文姓名"字段真正写进 Keycloak 用户的 `attributes.chineseName`
（而不是被静默丢弃，POST 仍返回 200），需要打开 Realm settings → User profile →
Unmanaged attributes → Enabled。**首次生产部署不建议开**——这是对 `quick` realm 的一次
realm 级配置改动，上线时应尽量只做"新增两个 client"这一件事，不碰任何现有 realm 配置
（详见 [`design.md` 7 节](design.md#7-已确认事项)）。不开这个开关不影响其他任何功能，
审计日志里的中文姓名一直会正确记录。

## 7. CloudFormation 部署

### 7.1 部署前核对

```bash
export AWS_PROFILE=xc

aws elbv2 describe-load-balancers --region us-east-1 \
  --query 'LoadBalancers[0].{ARN:LoadBalancerArn,DNS:DNSName}'
```

记下这里查到的真实 `LoadBalancerArn`（对应 `ExistingAlbListenerArn` 要传的
Listener ARN，不是 ALB 本身的 ARN，还需要 `aws elbv2 describe-listeners
--load-balancer-arn <上面查到的 ARN>` 拿 Listener 那一级）和现有 ALB 的
SecurityGroup ID（`ExistingAlbSecurityGroupId`）。

`infra/template.yaml` 里 `VpcId`/`AppSubnetId`/`AmiId`/`ExistingAlbListenerArn`/
`ExistingAlbSecurityGroupId` 这几个参数的 `Default` **都是占位符**（`vpc-xxx...`
这类），不是真实可用的值——这份模板已经做过脱敏处理，仓库里不保留真实的 VPC/子网/
安全组/AMI/ALB ARN。**部署时必须用 `--parameter-overrides` 把这几个参数覆盖成你
自己环境里的真实值**，用占位符直接部署会在 CloudFormation 侧报资源不存在的错误。

这个栈把现有 ALB 的 ARN 当字符串参数传入，**不建立 CloudFormation 跨栈依赖**，如果
现有栈的 ALB/Listener 被替换（哪怕是无关改动误触发），这个栈不会有任何报错提示，
只会静默指向失效 ARN。§7.1 这条核对检查因此必须在**每次**要变更现有 ALB/Listener
所在栈之前重做一遍，不只是首次部署时做一次。

### 7.2 部署命令

```bash
aws cloudformation deploy \
  --template-file infra/template.yaml \
  --stack-name quick-account-manager \
  --capabilities CAPABILITY_IAM \
  --region us-east-1 \
  --parameter-overrides \
    VpcId=<你的真实 VPC ID> \
    AppSubnetId=<你的真实子网 ID> \
    AmiId=<你的真实 AL2023 arm64 AMI ID> \
    ExistingAlbListenerArn=<§7.1 查到的真实 Listener ARN> \
    ExistingAlbSecurityGroupId=<现有 ALB 的 SecurityGroup ID> \
    AdminDomainName=quick-admin.yourdomain.com \
    AlarmNotificationEmail=your-email@example.com
```

### 7.3 关键参数说明

| 参数 | 默认值 | 何时需要改 |
|---|---|---|
| `VpcId` / `AppSubnetId` | 占位符（脱敏），复用 Keycloak 那台实例所在的默认 VPC/子网 | **每次部署都必须覆盖**成真实值，占位符不可用 |
| `InstanceType` | `t4g.medium` | 对齐现有 Keycloak 实例规格，一般不改 |
| `AmiId` | 占位符（脱敏），实际应锁定字面量 AL2023 arm64 AMI ID | **每次部署都必须覆盖**成真实值；要升级 AMI 就重新解析一次新的 AMI ID 并评估重建影响——**不要**改成 `{{resolve:ssm:...}}` 动态解析写法，会在任何一次不相关的模板更新时把实例整个替换掉（此前踩过的教训） |
| `ExistingAlbListenerArn` / `ExistingAlbSecurityGroupId` | 占位符（脱敏） | **每次部署都必须覆盖**成 §7.1 查到的真实值 |
| `AdminDomainName` | `quick-admin.example.com`（示例域名） | 改成你自己的真实域名 |
| `AlarmNotificationEmail` | 空 | 留空则 SNS Topic 仍会建、Alarm 仍会触发，但没人收到邮件通知；建议部署时就填，之后也可以去 SNS 控制台手动补订阅 |

### 7.4 部署产出

栈会新建：一台独立 `t4g.medium` EC2（不开 SSH，走 SSM）、一个新 TargetGroup + 挂在
现有 ALB `HttpsListener` 上的 host-header ListenerRule、私有 S3 桶（代码分发用）、
CloudWatch Alarm + SNS Topic。用 `aws cloudformation describe-stacks --stack-name
quick-account-manager --query 'Stacks[0].Outputs'` 拿到 `InstanceId`/`AppCodeBucketName`
等输出值，后面几步会用到。

**预期会看到的告警，不是故障**：`UserData` 只装 Docker，不会自动拉代码/起容器，栈刚
建完到手动部署完成这段时间 `/health` 连不通，`UnhealthyTargetAlarm` 会正常触发一次
SNS 通知，完成 §8 之后自动恢复。

## 8. 部署后人工步骤

### 8.1 加 CNAME

在你的域名解析商那边加一条 CNAME：`AdminDomainName` 参数的值（比如
`quick-admin.yourdomain.com`）→ 现有 ALB 的 DNS 名。ALB DNS 名不是本栈的资源，
`Outputs.Step1DNS` 只给了查询提示，真实值用 `aws elbv2 describe-load-balancers`
查（跟 §7.1 用的是同一条命令），应该跟现有员工 SSO 那条 CNAME 指向同一个 ALB。
如果现有证书是通配符证书（`*.yourdomain.com`），不需要新建证书。

### 8.2 写 SSM Parameter Store

```bash
aws ssm put-parameter --name /quick-account-manager/keycloak-service-client-secret \
  --type SecureString --value "<真实值>"
aws ssm put-parameter --name /quick-account-manager/keycloak-oidc-client-secret \
  --type SecureString --value "<真实值>"
aws ssm put-parameter --name /quick-account-manager/session-secret-key \
  --type SecureString --value "$(openssl rand -hex 32)"
```

实例 IAM Role 已经有 `/quick-account-manager/*` 路径的只读权限（`infra/template.yaml`
`AppInstanceRole`）。这些值最终还是要落进实例上的 `.env` 文件（本项目目前没有做
"启动时从 SSM 拉取拼 `.env`"的自动化，是 §8.4 手动把 `.env` 传上去这一步里手动填入
真实值——写进 SSM 是为了不让真实 secret 出现在任何命令行历史/仓库里，取值后手动
粘贴进 `.env`）。

### 8.3 代码分发（走 S3，不依赖代码托管平台）

不管这个仓库有没有推到 GitHub、公开还是私有，部署流程都走 S3 中转，不在实例上
配置任何代码托管平台的凭证——实例 IAM Role 只拿到这个 S3 bucket 的只读权限：

```bash
git archive --format=tar.gz -o release.tar.gz HEAD   # 只含 git 已提交文件，不含 .env/.venv/data/
aws s3 cp release.tar.gz s3://<Outputs.AppCodeBucketName>/release.tar.gz
```

### 8.4 登录实例，起容器

走 SSM Session Manager（不是真 SSH）：

```bash
aws ssm start-session --target <Outputs.InstanceId>
```

实例上：

```bash
sudo aws s3 cp s3://<AppCodeBucketName>/release.tar.gz /opt/quick-account-manager/release.tar.gz
cd /opt/quick-account-manager
sudo tar xzf release.tar.gz
sudo vi .env    # 按 §9 环境变量清单填真实值（生产环境 ENVIRONMENT 保持默认 production）

sudo docker build -t quick-account-manager:latest -f deploy/Dockerfile .
sudo docker volume create qam-data
sudo docker run -d --name qam-app --restart unless-stopped \
  -p 8000:8000 --env-file .env -v qam-data:/app/data \
  quick-account-manager:latest
```

**为什么不用 `docker compose up --build`**：见 §8.5 已知坑。

### 8.5 已知坑：AL2023 上 `docker compose --build` 跑不通

首次生产部署实测踩到：AL2023 的 `dnf` 仓库里没有 `docker-buildx-plugin`，装不上，而
`docker compose up --build`（Compose v5.5.0+）会硬性要求 buildx 存在，报错
`compose build requires buildx 0.17.0 or later`，直接卡住起不来容器。

**解法**：绕开 `docker compose`，改用经典 `docker build` + `docker run`（效果等价，
读同一个 `.env`、同一个 `Dockerfile`），即 §8.4 里的命令。`deploy/docker-compose.yml`
继续保留给本地开发用（本地环境一般有更新的 Docker Desktop/buildx，不会踩这个坑）。

以后想在生产也走 `docker compose`，需要单独装 buildx（AL2023 没有现成 dnf 包，得从
[docker/buildx releases](https://github.com/docker/buildx/releases) 下二进制放到
CLI 插件目录），目前没做，不阻塞上线。

### 8.6 已知坑：CFN `GroupDescription` 只支持 ASCII

`AWS::EC2::SecurityGroup` 的 `GroupDescription` 字段是 EC2 API 层面的限制，只支持
ASCII——中文说明会被 EC2 直接拒绝创建，报错 `Character sets beyond ASCII are not
supported`，`aws cloudformation validate-template` 的语法校验测不出这个问题（它只
校验模板语法，不校验运行时字段值域）。`infra/template.yaml` 里 `AppSecurityGroup` 的
`GroupDescription` 因此写的是英文，中文说明放在旁边的注释里。**如果以后要改这个字段，
保持英文**。

## 9. 环境变量清单

| 变量 | 本地开发 | 生产要求 |
|---|---|---|
| `ENVIRONMENT` | `development`（允许占位 secret 跑起来） | `production`（默认值；占位 secret 或短于 32 位会被启动校验拒绝） |
| `KEYCLOAK_BASE_URL` | 可指向测试 realm | `https://sso.example.com` |
| `KEYCLOAK_REALM` | — | `quick` |
| `KEYCLOAK_SERVICE_CLIENT_ID` / `KEYCLOAK_SERVICE_CLIENT_SECRET` | 假值即可 | §6.1 建的 client，secret ≥ 32 位真随机值 |
| `KEYCLOAK_OIDC_CLIENT_ID` / `KEYCLOAK_OIDC_CLIENT_SECRET` | 假值即可 | §6.2 建的 client，secret ≥ 32 位真随机值 |
| `OIDC_REDIRECT_URI` | `http://localhost:8000/auth/callback` | `https://quick-admin.example.com/auth/callback` |
| `ADMIN_GROUP_PATH` | `/quick-admin-pro` | 一般不改 |
| `SESSION_SECRET_KEY` | 任意 | `openssl rand -hex 32` 生成，≥ 32 位 |
| `DATABASE_URL` | `sqlite:///./data/app.db` | 一般不改（SQLite 落在容器挂载的 `qam-data` volume 里） |
| `AWS_REGION` / `AWS_ACCOUNT_ID` | 可留默认 | `us-east-1` / 实际账号 ID，用于 QuickSight 只读核对，不配也能跑，只是订阅状态显示"未知" |

`ENVIRONMENT`/两个 client secret/`SESSION_SECRET_KEY` 由 `app/core/config.py` 在启动
时强制校验——非 `development` 环境下这几个字段等于已知占位符或短于 32 位会直接拒绝
启动（`ValueError`），这是刻意设计：宁可部署失败也不带着假密钥上线。

## 10. 验证部署成功

1. `curl https://quick-admin.example.com/health` 返回 `{"status": "ok"}`
2. 用 `/quick-admin-pro` 组里的真实账号访问 `https://quick-admin.example.com/`，
   应该跳转 Keycloak 登录页，登录成功后回到用户列表页
3. 用一个**不在** `/quick-admin-pro` 组里的账号登录，应该看到 `/auth/forbidden` 的
   403 页面（验证 fail-closed 权限检查生效）
4. AWS 控制台确认 `UnhealthyTargetAlarm` 已经从 ALARM 状态恢复成 OK
5. 建一个测试账号（用低频档位，比如"作者版"），确认能看到一次性密码、用户列表里能
   看到这个新用户、审计日志里有对应记录

## 11. 更新代码 / 回滚

**更新**：重复 §8.3-§8.4——重新打包上传 S3，实例上重新拉取、`docker build` 打新
镜像、`docker stop qam-app && docker rm qam-app` 后用同样的 `docker run` 命令重新起
（`--env-file .env` 保持不变，`qam-data` volume 保留，数据不丢）。

**回滚**：`git archive` 时指定回退到的历史 commit（`git archive --format=tar.gz -o
release.tar.gz <commit-sha>`），其余步骤相同。数据库 migration 如果引入了新表结构，
回滚代码前先确认 `alembic downgrade` 到对应版本，或确认新旧代码都能兼容当前表结构
（本项目目前只有 `audit_log` 一张表，历史上只做过新增列的 migration，兼容性风险低）。

CloudFormation 栈本身如果要更新（比如改 `AlarmNotificationEmail`），照常
`aws cloudformation deploy` 重跑；`EC2Instance` 的 `DeletionPolicy`/
`UpdateReplacePolicy` 都设了 `Retain`，误操作也不会丢实例数据。

## 12. 备份现状

`design.md` 5 节规划的 AWS Backup Plan（EBS 快照）**目前没有写进 CloudFormation
模板**——明确标注为"上线后再补，不阻塞 MVP"，不是遗漏。生产环境上线后应尽快补上：
`AWS::Backup::BackupPlan` + `AWS::Backup::BackupSelection` 声明式配置，不需要自定义
脚本。在补上之前，`qam-data` volume 里的 SQLite 文件（审计日志）没有自动化备份。

## 13. 相关文档

- [`architecture.md`](architecture.md) —— 系统架构
- [`design.md`](design.md) —— 完整设计决策记录
- [`user-guide.md`](user-guide.md) —— 面向管理员的操作手册
