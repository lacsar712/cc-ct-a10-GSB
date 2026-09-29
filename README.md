# 数控刀补复核台

操作员提交刀具编号与刀补微米值；后台 worker 用 PostgreSQL 行锁（`select_for_update(skip_locked=True)`）认领待复核记录，按绝对值是否不超过 12 微米给出「合格」或「超差」。

## 占位超时回收

单子被认领进入「复核中」即视为**占位**。系统支持最长占位时长管控：

- **写权限员（machinist）**可在「占位回收台」设置最长占位秒数。单子进入复核中的瞬间把当时的时长**快照**到该单（`hold_seconds`），事后改时长只作用于**之后新进入**复核中的单，已经占着的单不追溯。
- worker 的回收线程每 0.5 秒扫描一次：占位超过单内快照时限仍未写出结论的单，被**自动吐回「待复核」**并在**回收账**落一条记录（刀具、占入时间、适用秒数、吐回时间、原因）。
- **写结论与回收互斥，只许一种结局**：两者都在数据库行锁内做条件更新（`WHERE status='processing'`）。写结论先提交则状态为已完成、回收条件更新命中 0 行、不记账；回收先提交则回到待复核并记账，写结论方查到 0 行直接放弃，绝不残留「复核中 + 半截结论」。
- **占位监视 / 回收账 / 真实状态可对账**：`GET /api/holds/reconcile` 复核三方一致性，页面顶部以「账实一致 / 账实不符 N 条」徽标实时呈现。
- **只读员（auditor）**可翻查时长设置、占位监视、回收账与对账结果，但不能改时长、不能触发回收（接口返回 403）。

入口：登录后顶部导航「**占位回收台**」（`#/reclaim`），页内**时长设置 / 占位监视 / 回收账三块并排**，每 2 秒自动刷新并显示实时倒计时。

### 验收：极短时长 + 故意占位

1. machinist 登录，进入「占位回收台」，在**时长设置**把最长占位改为 `2` 秒并保存。
2. 在**占位监视**区块用「占住不写结论」提交一条（该单直接进入复核中、快照 2 秒、但不写结论，确定性复现占位）。
3. 约 2 秒后可见：该单从占位监视消失、被吐回「待复核」（随后会被正常复核），**回收账多出一条**，顶部保持「账实一致」。
4. 用 auditor 登录进入同一页：三块数据都能看，但没有保存时长、占位、回收按钮；直接 `PUT /api/hold-config` 返回 403。

也可通过 worker 环境变量 `WORKER_HOLD_SECONDS=<秒>` 让认领线程在写结论前故意等待，制造写结论与回收的真实竞速。

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Django 5 + django-ninja（ASGI / uvicorn） |
| 前端 | SolidJS + Vite，nginx 反代 `/api` |
| 数据库 | PostgreSQL 16 |
| 鉴权 | JWT（python-jose），令牌存浏览器 localStorage |

## 端口

| 服务 | 地址 |
|------|------|
| 页面 | http://localhost:3196 |
| 接口 | http://localhost:8196 |
| PostgreSQL | localhost:54396（库名 `cncoffset`） |

## 账号

| 用户 | 密码 | 权限 |
|------|------|------|
| machinist | machine123456 | 可提交刀补、可设置占位时长、可模拟占位/手动回收 |
| auditor | audit123456 | 只读：列表、时长设置、占位监视、回收账、对账均可读不可改 |

## 启动

```bash
cd projects/17-cnc-tool-offset-desk
docker compose up --build
```

健康检查：`GET http://localhost:8196/api/health` → `{"status":"ok"}`

## 验收

1. machinist 登录后，种子数据应显示刀具 T01 合格（刀补 5 µm）、T09 超差（刀补 20 µm）。
2. 提交一条新刀补后，状态先为「待复核」，数秒内 worker 处理为「已完成」并给出结论。
3. auditor 登录后只能看列表，没有提交表单。
4. 按上文「验收：极短时长 + 故意占位」观察超时单自动吐回待复核、回收账 +1、账实一致。

## 主要接口（均需 Bearer JWT，除 health/login）

| 方法 | 路径 | 权限 | 说明 |
|------|------|------|------|
| GET | `/api/hold-config` | 登录可读 | 当前最长占位秒数、更新人/时间 |
| PUT | `/api/hold-config` | 仅写权限员 | 修改最长占位秒数（只对新占位生效） |
| GET | `/api/holds` | 登录可读 | 占位监视列表（含剩余秒数、是否超时） |
| POST | `/api/holds/reclaim` | 仅写权限员 | 手动触发一轮超时回收 |
| POST | `/api/holds/simulate` | 仅写权限员 | 造一条进入复核中却不写结论的单（验收用） |
| GET | `/api/holds/reconcile` | 登录可读 | 监视/账/真实状态三方对账 |
| GET | `/api/reclamations` | 登录可读 | 回收账 |
| POST | `/api/submissions/{id}/hold` | 仅写权限员 | 把一条候审单置为复核中占位（验收用） |

运维也可在 backend 容器内执行 `python manage.py reap_holds` 手动回收一轮。

## 目录

```text
backend/          Django 工程（config/、desk/、worker.py）
frontend/         SolidJS 单页
docker-compose.yml
PRD.md
```
