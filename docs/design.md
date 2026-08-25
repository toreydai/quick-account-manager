# quick-account-manager 设计文档

状态：规划中，尚未开始实现。本文档只做架构/基础设施规划，不含可运行代码。

## 1. 背景

`xuechuan-quick-sso`（[相关目录](../../xuechuan-quick-sso)）已经跑通了 Keycloak → SAML → IAM Role → Amazon Quick 的 SSO 链路，34 人已经用 `add_single_user.py` / `create_users_from_xlsx.py` 建号。但建号流程本身还是纯命令行：

- 管理员密码每次 `getpass` 手输，没有服务账号
- 密码明文写进同目录 `quick-sso-users-batch1.xlsx`，人工转发
- 没有操作审计（谁在什么时候给谁开了什么档位，只能翻 xlsx 或 Keycloak 日志）
- 只有会用命令行的人能操作

参考 [kiro-fleet](https://github.com/toreydai/kiro-fleet)（同类问题在 Kiro/IAM Identity Center 场景下的 Web 化方案）的模式，把 `add_single_user.py` 的建号逻辑包一层带权限控制和审计的 Web UI。**跟 kiro-fleet 相比这里的场景小得多**（单账号单 realm、订阅是固定月费档位不是 credit 消耗制），所以不照搬其多账号/Credit 统计/Token 导出那套复杂度，只做这个场景真正需要的子集。

## 2. 目标与范围

### MVP 范围内

- Web 登录：走 Keycloak OIDC，不单独建一套账号密码表——复用 `quick` realm 现有的 `/quick-admin-pro` 组做管理员身份源，组里已经有 3 个真实账号（`alice`/`bob`/`carol`），应用一上线就能登录，不需要额外 bootstrap 一个初始管理员账号（详见 4.4）；MFA 留作后续迭代，MVP 不做——人数少、内网访问为主，已与用户确认这个取舍
- 建单个 Quick SSO 账号：邮箱/拼音姓名/角色档位。**全部 6 档都管**（2026-08-20 用户明确要求，推翻了最初"只开放作者两档"的设计决定——起因是生产上线后，用户在真实用户列表里看到某个真实管理员账号显示"不在本应用管理范围内"，明确要求所有人都要能被管，不能有例外）。`ROLE_TO_GROUP` 覆盖管理员/作者/阅读者的专业版与非专业版共 6 档，映射关系对齐 `xuechuan-quick-sso/admin-guide-add-users.md` 里 `create_users_from_xlsx.py` 的完整表格
- **安全边界**：`/quick-admin-pro` 组同时是登录本应用的门槛（4.4 节），开放管理员档位的改档位/停用之后，理论上任何管理员都能把最后一个 admin-pro 降级/停用，导致所有人（包括操作者自己）都进不去这个工具，也没法再用工具自己修——这是这次扩权限直接引入的真实风险，不是假设性的。已加两条防护：①不允许把 `/quick-admin-pro` 组降到 0 人（`keycloak_service.LastAdminGuardError`）；②不允许管理员通过本应用停用/改自己的档位（`SelfLockoutError`，防呆不防坏人）
- 建号后一次性展示初始密码（页面刷新/离开即不可再查看），管理员自行转发给本人
- 用户列表：不维护本地副本，实时查 Keycloak `GET /admin/realms/quick/users`（+ 只读 QuickSight API 核对订阅状态），保证现有 34+ 个用 CLI 脚本建的号从上线第一天就完整可见，不会因为本应用只记录"自己建的号"而漏掉历史数据；状态区分「已建号 / 待首次登录激活订阅」（订阅由 `QuickSubscriptionAssignFunction` 异步生效，不是本应用能替用户完成的事）
- **管理员重置指定用户密码**：从用户列表选中一个人，一键生成新的临时密码（复用 4.2 的密码生成规则）并写回 Keycloak，同样一次性展示、不落库；权限上不需要额外申请——4.1 给后端 service account 分配的 `manage-users` client role 本来就包含重置密码。这个功能直接替代"密码展示失败 / 员工忘密码只能去 Keycloak 控制台手动重置"这个原本的人工兜底路径（详见 5 节）
- **订阅档位变更**：给已建号的用户在全部 6 档之间任意切换（从旧 Keycloak 组移除、加入新组），2026-08-20 起不再限定只能在作者两档之间切换
- **账号停用**：员工离职场景，禁用该用户在 Keycloak 里的账号（`enabled: false`），而不是硬删除——保留历史记录和审计可追溯性，也符合可逆操作优先的原则；彻底删除留作后续迭代方向，不纳入 MVP（见 4.5）
- 操作审计日志（谁、什么时候、给谁开了什么档位；重置密码、变更档位、停用账号都算一类操作，一并记录）

### 明确不做（相对 kiro-fleet 裁掉的部分）

| kiro-fleet 有 | 这里不做的原因 |
|---|---|
| 多账号 Vault、跨账号总览 | 只有一个 AWS 账号一个 Keycloak realm |
| Credit 用量统计 | Quick 订阅是固定月费档位，不是 credit 消耗制，没有对应指标 |
| Trusted Token Issuer 账户 JSON 导出 | Quick 走浏览器/Desktop SAML+OIDC 登录，不需要签发 access token 给客户端 |

批量导入（xlsx 上传 → 预览校验 → 确认后逐个建号，表头跟 `create_users_from_xlsx.py`
一致）已在 MVP 之后补上，见 `app/services/batch_import.py` + `app/api/users.py` 的
`/users/batch-create*` 三个路由；MFA、自动邮件通知仍留作后续迭代方向。

**2026-08-24 又补上一批，参考 kiro-fleet 现有功能盘点后拉的清单**（多账号/Credit/
Token 导出那三项仍然不做，理由不变，见上表）：

- **批量改档位 / 批量停用启用**：用户列表页每行加勾选框，选中多个用户后在批量
  操作栏里一次性切换档位或停用/启用，见 `/users/batch-change-tier`、
  `/users/batch-set-enabled` 两个路由。跟单用户版本共享同一套 `KeycloakService.
  change_tier`/`set_enabled`，逐行调用（不并发，理由同批量建号）、逐行记审计日志、
  逐行返回成功/跳过/失败，某一行失败不影响其它行继续跑。安全边界照抄单用户版本：
  批量改档位/停用如果选中了操作者自己，那一行会被跳过（`SelfLockoutError`），不
  会连累其它行；最后一个 admin-pro 保护（`LastAdminGuardError`）同理逐行生效。
- **批量建号实时进度**：`/users/batch-create/confirm` 从"提交后等一个同步返回的
  结果页"改成 `StreamingResponse`（`text/event-stream`），每建完一个人推一条 SSE
  事件，预览页（`batch_preview.html`）内联 JS 用 `fetch` + `ReadableStream` 手动
  解析（不是标准 `EventSource`——`EventSource` 只支持 GET，这里的表单数据必须走
  POST body），边收边把结果行插进页面，人数多的时候不再是提交后卡住没反馈。
- **审计日志过滤**：`/audit-log` 加了操作人邮箱/目标邮箱（模糊匹配）、操作类型、
  成功/失败四个查询参数，`audit_service.list_recent()` 对应加了可选过滤参数，
  纯本地 SQLite 查询，成本可以忽略。
- **硬删除用户**：4.5 节原来评估后决定留作后续迭代，现已启用——`KeycloakService.
  delete_user()` 调 `DELETE /admin/realms/quick/users/{id}`，路由层
  （`/users/{user_id}/delete`）在真正调用前有三层防护：不能删自己
  （`SelfLockoutError`，跟改档位/停用一致）、复用最后一个管理员保护（不能把
  quick-admin-pro 删空）、以及要求管理员在表单里手打一遍目标邮箱做二次确认
  （后端逐字核对，不接受模糊匹配），因为这个操作比"停用"风险高一个量级，
  浏览器原生的 `confirm()` 弹窗容易被无意识点掉。
- **审计日志写入容错**：以上几项对着本地真实 Keycloak 26.6.3 做端到端验证时意外
  发现的既有问题（不是这批新功能独有，单用户路由原来也这样，只是批量路由把影响
  放大了）——`audit_service.record()` 写库失败（DB 锁住/磁盘满/表结构对不上）原来
  会直接抛异常，单用户场景下只是这次操作报错文案不准确，但批量路由是在一个 for
  循环里逐个调用，未捕获的异常会打断整个循环：前面几行 Keycloak 操作已经真实生效
  但审计没记上，后面排队的行完全不被处理，管理员看到的是裸 500 页面。现在
  `record()` 内部 try/except 兜底，写失败就地 `db.rollback()` + 记一条 error 级别
  日志、返回 `None`，不再往上抛——调用方（41 处）都不使用这个函数的返回值，改成
  `Optional[AuditLog]` 不需要动任何调用点。代价：极端情况下（DB 真的写不进去）审计
  日志会漏记，但 Keycloak 操作本身的正确性不受影响，这个取舍是刻意的——"审计记录
  不完整"比"批量操作里后面的人全部没处理、还看不到任何解释"损失小得多。

**2026-08-24 又补一批（用户反馈"管理员删除有没有二次校验、手滑误删了怎么办"之后加的）**：

- **硬删除必须先停用**：`KeycloakService.delete_user()` 现在先查一次目标账号的
  `enabled` 状态，还是启用中就直接拒绝（`MustDisableFirstError`），不打 DELETE
  请求。这条规则放在 service 层而不是只在路由层查一次——即使换个调用方式也躲不掉，
  跟 `LastAdminGuardError` 的定位一致。目的不是技术上防绕过，是流程上强制一个
  "先停用、留观察窗口、确认没问题再删"的顺序：一个还在正常使用的账号被直接停用，
  本人会发现登不进去、有机会找管理员反馈；被直接硬删除则是静默消失，删错了也没人
  第一时间发现。代价：离职这类"确定要走"的场景，现在删除前必须多一步先停用的操作，
  但这本来就是这类操作该有的顺序，不是额外负担。
- **审计日志记录被删用户的原角色档位**：`delete_user` 路由的 `audit_service.record()`
  调用之前没传 `detail`，只知道"谁在什么时候删了谁"，不知道该重建成什么档位——现在
  `detail` 字段固定记 `target.role`（拿不到就记"（未分配档位）"），万一真的删错了，
  至少有据可查该恢复成什么样，不用凭记忆猜。
- **新建账号的角色范围单独收窄回两档**：跟上面两条不是一类问题，是同一批改动里
  顺带做的——`create_user()`（单人建号）和 `batch_import.parse_xlsx()`（批量导入）
  现在都用新常量 `CREATE_ALLOWED_ROLES = ("作者专业版", "作者版")` 校验，不再是
  `ROLE_TO_GROUP` 全 6 档。**这不是推翻 2026-08-20"全部 6 档都管"的决定**——那次
  决定针对的是"列表页管理范围"（改档位/停用/删除，`ROLE_TO_GROUP`/`VALID_ROLES`
  不变，还是全 6 档），这次收窄的只是"新建账号"这一个高频操作的可选项：管理员/
  阅读者这几档目前都是内部已有账号手动走 Keycloak 控制台开的特殊情况，不该让
  "新建"这个下拉框把这些低频、需要额外判断的档位也列出来，增加选错的可能性。
  已建号的人如果要改到这两档之外，用列表页的「切换档位」（不受影响，还是全 6 档）。
- **批量建号 xlsx 模板下载**：`GET /users/batch-create/template`（`batch_import.
  build_template_xlsx()`），表头 + 一行示例 + role 列加 Excel/WPS 原生下拉数据验证
  （限定 `CREATE_ALLOWED_ROLES` 这两档），减少管理员手填角色名打错字/打出不支持
  档位的情况——这类错误原来要等上传后在预览页才会被标红，模板里的下拉能在填表
  这一步就先挡掉一部分。

## 3. 部署架构

**独立 EC2，复用现有 ALB。** 不与 Keycloak 共用实例，理由：故障域隔离（新应用的 bug/资源占用不影响生产 SSO），也不用改动、更不用重新部署现有 `xuechuan-quick-sso` 那个 CloudFormation 栈。

```
                        ┌─────────────────────────────┐
   HTTPS (443)          │   现有 ALB（your-existing-alb-...） │
   ─────────────────►   │   *.example.com 通配符证书 │
                        └───────────┬─────────────┬────┘
                                    │             │
                     Host: awssso...│             │Host: quick-admin...（新增）
                                    ▼             ▼
                        ┌───────────────┐   ┌───────────────────┐
                        │ 现有 TargetGroup│   │ 新 TargetGroup（新增） │
                        │  → :8080       │   │  → :8000            │
                        └───────┬───────┘   └─────────┬─────────┘
                                ▼                       ▼
                  ┌─────────────────────┐   ┌─────────────────────────┐
                  │ 现有 EC2             │   │ 新 EC2（本项目）           │
                  │ Keycloak + Postgres  │   │ quick-account-manager    │
                  │ i-xxxxxxxxxxxxxxxxx  │   │ app + db                 │
                  └─────────────────────┘   └───────────┬─────────────┘
                                                          │ HTTPS (Admin REST API,
                                                          │ client_credentials)
                                                          ▼
                                              Keycloak（同 VPC，走域名或内网直连）
```

新应用通过 HTTPS 调 Keycloak 的 Admin REST API（走 `sso.example.com` 或直接内网访问同一 VPC 里 Keycloak EC2 的 8080，两者都可行，倾向走对外域名以复用现成的健康检查和 TLS，避免额外配置内网直连信任关系），不需要两台 EC2 之间开特殊网络规则。

### 3.1 ALB 复用方案

**独立 CloudFormation 栈，不修改 `xuechuan-quick-sso` 现有栈/模板。** 现有 `LoadBalancer`/`HttpsListener` 的 ARN 作为新栈的输入参数（字符串直接传入或用 `Fn::ImportValue`，取决于现有栈是否愿意加 `Outputs`/`Export`——不改现有栈的话就直接把 ARN 当参数字面量传，`ListenerRule`/`TargetGroup` 资源本来就不要求和 Listener/ALB 在同一个栈里）。这样新栈的任何变更（包括误操作）都不可能波及现有 Keycloak 生产栈，这也是部署记录里吃过一次"execute-change-set 被判定拒绝但实际执行了"的教训后应该有的隔离。

**已知耦合风险**：把 ARN 当字面量参数传，CloudFormation 不会把这当依赖关系跟踪——如果现有栈的 ALB/Listener 以后被替换（哪怕是无关改动误触发的替换），新栈的 `ListenerRule` 会静默指向失效 ARN，不会有任何 CFN 层面的报错提示。不为这个低概率场景上自动化检测（ALB 是长期存活资源，被替换概率本来就低），改成一条运维检查清单项：**以后任何一次要变更 `xuechuan-quick-sso` 现有栈，操作前先 `aws elbv2 describe-load-balancers` 核对 ALB/Listener ARN 有没有变，再动这个新栈。**

新增资源（都在新栈里）：

| 资源 | 说明 |
|---|---|
| `TargetGroup`（新） | Port 8000，Protocol HTTP，`TargetType: instance`，`HealthCheckPath: /health`（应用需要提供这个端点），Targets 指向新 EC2 |
| `ListenerRule`（新） | 挂在现有 `HttpsListener`（443）上，`Conditions: [{Field: host-header, Values: [quick-admin.example.com]}]`，`Actions: [{Type: forward, TargetGroupArn: 新TargetGroup}]`。用 host-header 而不是 path-based 分流——避免跟 Keycloak 自己的 `/admin` 路径产生任何歧义 |
| `AppSecurityGroup`（新） | 只放行 ALB 的 SecurityGroup → 8000，不对公网开放任何端口 |
| `EC2Instance`（新） | 见 3.2 |

不需要新建证书：`*.example.com` 通配符证书（ACM ARN `arn:aws:acm:us-east-1:123456789012:certificate/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`）已经覆盖任意子域名。

需要人工操作：在阿里云 HiChina 加一条新 CNAME，`quick-admin.example.com` → 现有 ALB DNS 名（跟现在 `awssso` 那条 CNAME 指向同一个 ALB，两条 CNAME 共用一个 ALB 是标准做法）。

### 3.2 新 EC2 规划

| 项 | 建议值 | 理由 |
|---|---|---|
| InstanceType | `t4g.medium` | 对齐现有 Keycloak 实例规格，一次到位不用后续调参数（已与用户确认：接受初期可能资源冗余的取舍，换取不用后续再改）|
| ImageId | 显式 `AWS::EC2::Image::Id` 参数，锁定字面量 AMI ID，不用 `{{resolve:ssm:...}}` 动态解析 | 直接照搬 `xuechuan-quick-sso` 踩过的教训（[部署记录第 20-21 节](../../xuechuan-quick-sso/deployment.md)）：动态解析 AMI 会在任何一次不相关的模板更新时把实例整个替换掉 |
| VPC/子网 | 复用同一个默认 VPC（`vpc-xxxxxxxxxxxxxxxxx`），建议放 `PublicSubnet2`（跟 Keycloak 那台分开子网，AZ 级别再隔离一层，不是必需但成本为零） |
| SSH | **不开 22 端口，不建密钥对**。用 SSM Session Manager（IAM Role 挂 `AmazonSSMManagedInstanceCore`）做运维访问 | 比现有 Keycloak 那台"默认关 22、需要临时开安全组"更进一步——直接不留这个口子，减少一类攻击面，也更符合"声明式/托管服务优先"的取向 |
| DeletionPolicy / UpdateReplacePolicy | 两者都设 `Retain` | 同样照搬现有栈吃过教训后已经落地的保护 |
| IAM Role | 最小权限：`AmazonSSMManagedInstanceCore` + 自定义只读策略（`quicksight:ListUsers`、`quicksight:DescribeUser`、`quicksight:DescribeAccountSubscription`，`Resource` 限定到账号 `123456789012`） | 应用只需要读 QuickSight 用户/订阅状态做展示，不需要任何写权限——真正的建号写操作走 Keycloak Admin REST API，不经过 AWS API |

**成本量级**：`t4g.medium` 按需约 $24.5/月 + 16GB gp3 EBS 约 $1.3/月，合计约 **$26/月** 量级（us-east-1 on-demand 价格，未来可上 Savings Plan 再降）；ALB 复用现有资源，不产生增量费用。

### 3.3 监控告警

声明式方案，不写自定义监控脚本：CFN 模板里直接加 `AWS::CloudWatch::Alarm` 资源，盯两个指标——新 `TargetGroup` 的 `UnHealthyHostCount`、新 `EC2Instance` 的 `StatusCheckFailed`，触发后发 SNS Topic（订阅管理员邮箱）。跟 5 节的 EBS 快照备份一样，全部用托管服务的声明式配置表达，不需要额外运行时代码。

## 4. Keycloak 集成方案

### 4.1 认证：用服务账号取代人工输入 master admin 密码

现状（`add_single_user.py`）用的是 master realm 的人类管理员账号，`grant_type=password` 直连拿 token——这个方式本来就是给命令行工具临时用的，不适合被 Web 后端长期持有。

**改法**：在 Keycloak 的 `quick` realm（不是 `master`）里新建一个 confidential client，开启 Service Account，给这个 service account 分配 `realm-management` 的 client role：`manage-users`、`query-groups`、`view-users`（不要给 `manage-realm` 等更大权限）。后端用 `grant_type=client_credentials` 换 token，权限被限定在 `quick` realm 内部，即使这个凭证泄露也拿不到 master realm 的控制权——这是比现在的方案更小的权限半径。

这个 client 的创建是 Keycloak 侧的一次性手工配置（Admin Console 点几下，或者用现有 `bootstrap` 的 `python-keycloak` 库加几行做成幂等脚本，两种都行，不属于本应用的运行时代码）。

### 4.2 建号逻辑

密码生成规则（避开 `= + - @` 开头这几个坑）和 `requiredActions: []` 显式清空这两点直接复用 `add_single_user.py` 里验证过的逻辑。

**API 调用方式跟最初设计不一样，是实现阶段用真实 Keycloak 端到端测试才发现的**：`add_single_user.py` 用的 `partialImport` 接口能导入任意 realm 资源（client/role 等），Keycloak 把权限检查放在比 `manage-users` 更粗的粒度——4.1 节这个刻意收窄过的 service account（只有 `manage-users`/`query-groups`/`view-users`）打 `partialImport` 直接 403。`add_single_user.py` 当年能用是因为拿的是 master realm 人类管理员的完整权限，不是这里权限收窄过的 service account，这个权限粒度差异在设计阶段没有意识到，纯 mock 的单元测试也测不出来（403 只有对着真实 Keycloak 才会暴露）。

改成两步调用：`POST /users`（建号）+ `PUT /users/{id}/groups/{groupId}`（加组），都在 `manage-users` 权限范围内。两步之间失败的补偿逻辑（加组失败自动回滚删除、回滚也失败则报错但不让管理员误以为完全没建号）见实现里 `keycloak_service.py` 的 `create_user`，已经用真实 Keycloak 验证过失败场景。

**重置密码**复用同一套密码生成规则，调 `PUT /admin/realms/quick/users/{id}/reset-password`（`temporary: true`，走跟建号一样的 `manage-users` service account），不是新的权限面，只是同一个 client 的另一个 API 调用。

### 4.3 订阅生效是异步的

建号（Keycloak 建用户+加组）不等于订阅生效——订阅是用户首次登录 Quick 时由 `QuickSubscriptionAssignFunction`（监听 CloudTrail `CreateUser` 事件的 Lambda）自动分配的。Web UI 里用户状态要显式区分「已建号，待首次登录激活订阅」和「订阅已生效」（后者可以用只读 IAM 权限调 `quicksight:DescribeUser` 查 `Role` 字段核实），不能让管理员误以为点了"建号"订阅就已经开通。

### 4.4 应用自身登录：复用 Keycloak，不单独建账号体系

`quick-account-manager` 自己的管理员登录也走 Keycloak OIDC，不维护独立的用户名/密码表：

- 在 `quick` realm 新建一个 OIDC client（confidential，`amazon-quick-desktop` 那个 client 的配置可以直接参考），走标准 Authorization Code 流程
- 谁能登录这个管理后台，由 Keycloak 里的组成员关系决定——复用现有 `/quick-admin-pro` 组（当前 3 个真实账号：`alice`/`bob`/`carol`），后端只需要校验登录用户是否在这个组里（或对应 client role），不需要额外维护权限表
- **这样就不存在"第一个管理员账号怎么 bootstrap"的问题**——这 3 个账号已经真实存在，应用一上线就能直接登录，不需要在 `.env` 里塞一个初始管理员密码（对比 kiro-fleet 需要在 `.env.example` 里设初始管理员密码，这里因为复用了已有身份源而不需要这一步）
- 这个 OIDC client 跟 4.1 里给后端调 Admin REST API 用的 service account client 是两个不同的 client，职责分开：一个用来登录鉴权（`amazon-quick-desktop` 式的 OIDC），一个用来后端调 API（`client_credentials`，`manage-users` 权限）

**登录页视觉区分**：因为管理后台和 `sso.example.com` 的员工 SSO 登录走的是同一个 `quick` realm，中间跳转到 Keycloak 时两边会看到同一套登录页主题，容易让管理员一时分不清是在登哪个系统。用 Keycloak 原生的 client 级 theme 覆盖解决——给这个 OIDC client 的 `login_theme` 属性（`PUT /admin/realms/quick/clients/{id}`，`attributes.login_theme`）指定一个自定义主题（比如加一行"账号管理后台"标识/换个主色），登录页跳转到这个 client 时会用不同主题渲染，域名 + 登录页视觉双重区分。**待办**：这是 Keycloak 长期存在的能力，但要在实施阶段用当前部署的 26.6.3 版本实测确认（管理台 UI 不一定直接暴露这个字段，可能需要走 Admin REST API 或 `kcadm.sh` 设置），不确定的话作为一个开发阶段的验证项，不阻塞设计本身。

### 4.5 订阅档位变更与账号停用

跟重置密码一样，复用 4.1 的 `manage-users` service account，不新增权限面：

- **档位变更**（作者版 ⇄ 作者专业版）：`DELETE /admin/realms/quick/users/{id}/groups/{旧组id}` + `PUT /admin/realms/quick/users/{id}/groups/{新组id}`，两步操作原子性由后端 service 层保证（先加新组再删旧组，避免中间状态下该用户完全不在任何 `quick-author*` 组里）。变更后订阅档位本身不会立刻变——跟建号一样，实际生效要等下次登录触发 `QuickSubscriptionAssignFunction` 重新分配，UI 上要用跟建号同样的「待下次登录生效」提示，不能让管理员以为点了按钮订阅立刻变了
- **账号停用**：`PUT /admin/realms/quick/users/{id}`，`enabled: false`。禁用后该用户无法再通过 SSO 登录（Keycloak 层面直接拒绝），但 Keycloak 用户记录、Quick 侧历史数据都还在，可随时用同一个接口 `enabled: true` 恢复，比硬删除更安全
- **硬删除**（2026-08-24 已启用，原来评估后决定留作后续迭代）：`DELETE /admin/realms/quick/users/{id}`，不可逆操作，比"停用"多三层防护——不能删自己、复用最后一个管理员保护、要求手打目标邮箱二次确认（见上面"2026-08-24 又补上一批"）。仍然只做单用户删除，不做批量删除——不可逆操作故意不给"批量"这个放大误操作影响面的入口

## 5. 数据与状态

| 需求 | MVP 方案 | 备注 |
|---|---|---|
| 持久化存储 | SQLite，单文件放在新 EC2 的 EBS 卷上 | 几十到上百人规模用不上额外的 RDS，先用最简单的方案；如果后续要多副本/高可用再迁移 Postgres（RDS 或复用 Keycloak 那台的 Postgres 容器开新 DB 均可） |
| 密码交付 | 建号 / 重置密码成功后返回体里带一次性明文密码，前端展示后不再请求；后端**不落库明文密码**（只记录"已生成，已展示"这个事实） | 直接消掉现在 xlsx 里"一份文件能看到所有人密码"的风险面；**接受的风险**：如果响应在传输中丢失（网络抖动/前端崩溃），密码没有第二次展示的机会——这种情况下不用重新建号，在用户列表里对这个人点一次「重置密码」（2 节新增的功能）就能拿到新的一次性密码，不用再跳去 Keycloak 控制台手动操作，也是员工忘密码时的标准处理路径 |
| 审计日志 | 一张 `audit_log` 表：操作人、时间、目标邮箱、角色档位、结果 | 替代现在只能翻 xlsx/Keycloak 日志的现状 |
| 备份 | EBS 快照（`DeletionPolicy: Retain` 的卷 + 定期快照即可，不需要额外脚本，用 AWS Backup 的声明式 Backup Plan） | 呼应"优先声明式配置/托管服务"的偏好，不写自定义备份脚本 |

## 6. 安全边界小结

- 新 EC2 不暴露公网 IP 的任何入站端口给非 ALB 来源（只有 ALB SG → 8000 一条规则）
- 不开 SSH，运维走 SSM
- Keycloak 凭证从"人工输入的 master 管理员密码"降级为"权限收窄到 `quick` realm 内 `manage-users` 的服务账号"，凭证本身存 SSM Parameter Store（SecureString），不进代码仓库、不进环境变量明文文件
- QuickSight 侧 IAM 权限只读，建号写操作完全不经过 AWS API（只经过 Keycloak REST API），减少这台实例能对 AWS 账户造成的影响半径

## 7. 已确认事项

以下几项已跟用户过完，MVP 按这个走，不再是开放问题：

| 事项 | 决定 |
|---|---|
| MFA | MVP 不做，登录走 Keycloak OIDC（见下一行），MFA 留后续迭代 |
| 应用自身登录 | 复用 Keycloak `quick` realm 的 `/quick-admin-pro` 组做管理员身份源（OIDC），不单独建账号密码表，也不需要额外 bootstrap 第一个管理员（见 4.4） |
| 登录页视觉区分 | 给管理后台的 OIDC client 单独配 Keycloak `login_theme`，跟 `awssso` 员工 SSO 登录页区分开，避免管理员搞混两个入口（见 4.4，Keycloak 26.6.3 实际支持方式留待实施阶段验证） |
| 用户列表数据源 | 不维护本地副本，实时查 Keycloak `GET /admin/realms/quick/users`，保证历史建号数据从上线第一天就完整可见 |
| 密码交付 | UI 建号后一次性展示明文密码，管理员人工转发给本人；不接自动通知（企业微信/邮件）；展示失败或员工忘密码，都在应用里点「重置密码」处理，不用再跳去 Keycloak 控制台，见第 5 节 |
| 重置密码功能 | 纳入 MVP 范围：管理员在用户列表里对指定用户一键重置密码，复用建号已有的 `manage-users` 权限和密码生成规则，不新增权限面（见 2 节 / 4.2 节） |
| 角色档位范围 | **2026-08-20 变更**：全部 6 档都管，推翻了最初"只开放作者两档"的决定；同时加了「最后一个管理员保护」和「防自锁」两条安全边界，见 2 节 |
| 订阅档位变更 | 纳入 MVP 范围：已建号用户可在全部 6 档之间任意切换，复用 `manage-users` 权限，变更后实际生效仍要等下次登录触发 Lambda（见 4.5） |
| 账号停用 | 纳入 MVP 范围：`enabled: false` 禁用而非硬删除，可逆、保留审计记录；硬删除原本留后续迭代，2026-08-24 已启用（带二次确认防呆），见 4.5 |
| 新 EC2 规格 | `t4g.medium`，对齐现有 Keycloak 实例，不从 micro 起步 |
| Unmanaged attributes | 首次生产部署**不开**这个 realm 级开关——用户明确要求上线过程对现有 `quick` realm 只做"新增两个 client"这一件事，不碰任何 realm 级配置；中文姓名功能因此在 Keycloak `attributes` 侧会被静默丢弃，但审计日志不受影响，一直可靠。后续如果要让中文姓名也在 Keycloak 侧生效，是一个独立的、需要重新评估影响面的决定，不在这次上线范围内 |
| Git 管理 | 从项目一开始就 `git init` 纳入版本管理，不重复 `xuechuan-quick-sso` 目前不在任何仓库里的问题 |

## 8. 相关文档

- [xuechuan-quick-sso/admin-guide-add-users.md](../../xuechuan-quick-sso/admin-guide-add-users.md) — 现有批量建号流程
- [xuechuan-quick-sso/add_single_user.py](../../xuechuan-quick-sso/add_single_user.py) — 单人建号逻辑，本项目 MVP 的建号 service 直接迁移自此
- [xuechuan-quick-sso/configuration.md](../../xuechuan-quick-sso/configuration.md) — 现有 ALB/EC2/Keycloak 具体参数值
- [xuechuan-quick-sso/deployment.md](../../xuechuan-quick-sso/deployment.md) — 现有栈的部署记录，含 AMI 漂移/EC2 误替换的教训（第 20-21 节）
- [kiro-fleet](https://github.com/toreydai/kiro-fleet) — 同类问题在 Kiro/IAM Identity Center 场景下的参考实现
