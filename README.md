# 数控刀补复核台

操作员提交刀具编号与刀补微米值；后台 worker 用 PostgreSQL 行锁（`select_for_update(skip_locked=True)`）认领待复核记录，按绝对值是否不超过 12 微米给出「合格」或「超差」。

## 占位超时回收

单子进入「复核中」即被某个复核员**占位**。占位回收台（导航「占位回收台」）提供三块并排视图：

1. **最长占位时长**：写权限员（操作员）可设置最长占位秒数（1–86400）。改时长**只作用于此后新进入复核中的单**；认领时会把当时的时限快照到单子上，已占着的单按各自快照时限执行，**不追溯**。
2. **占位监视**：实时（每秒）列出所有复核中的单——占位起始时刻、适用时限、已占位/剩余秒数，超时高亮。
3. **回收账**：每发生一次「复核中超时被吐回待复核」就记一笔，并给出**账实对账**结果（占位列表、回收账、每单回收计数三者必须相符）。

机制要点：

- 单独的 **reclaimer 进程**周期扫描，把超过各自快照时限仍未写结论的单吐回「待复核」、清空占位、回收计数 +1、同事务写回收账。
- **写结论与回收互斥**：两者都先 `SELECT ... FOR UPDATE` 锁行再复查，先提交者赢——成功写结论就不会再被回收；成功回收就不会残留「复核中」半截状态，也不会重复记账（`(submission, claimed_at)` 唯一约束兜底）。worker 醒来若发现占位已被回收，其结论作废丢弃。
- 被吐回的单回到待复核队列，可被再次认领；再次超时会再记一笔。

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
| machinist | machine123456 | 可提交刀补、**可改最长占位时长** |
| auditor | audit123456 | 只读：可看列表、时长设置、占位监视与回收账，**不能改时长**（接口 403） |

## 启动

```bash
cd projects/17-cnc-tool-offset-desk
docker compose up --build
```

服务除 `db / backend / frontend / worker` 外，还包含一个 **reclaimer**（超时回收进程）。
worker 认领后会占位 `WORKER_HOLD_SECONDS`（默认 5 秒）再写结论，便于在回收台观察「复核中 → 超时吐回」。

健康检查：`GET http://localhost:8196/api/health` → `{"status":"ok"}`

## 验收

1. machinist 登录后，种子数据应显示刀具 T01 合格（刀补 5 µm）、T09 超差（刀补 20 µm）。
2. 提交一条新刀补后，状态先为「待复核」，数秒内 worker 处理为「已完成」并给出结论。
3. auditor 登录后只能看列表，没有提交表单。
4. **超时回收**：进入「占位回收台」，machinist 把最长占位时长调到极短（如 2 秒），提交一条新刀补并让其一直占着（worker 占位时长调长即可），应看到该单从占位监视中消失、回到列表的「待复核」，且回收账多一条、对账始终「账实相符」。
5. **不追溯**：先用较长时长占一单，再把时长改短，已占单仍按原时长倒计时，此后新占的单才用新时长。
6. **只读员**：auditor 能翻时长设置与回收账，但保存时长返回 403、值不变。

后端并发/账实一致性的可重复校验脚本：`backend/verify_reclaim.py`（对真实 PostgreSQL 跑，含多轮写结论↔回收竞态断言）。

## 目录

```text
backend/          Django 工程（config/、desk/、worker.py、verify_reclaim.py）
frontend/         SolidJS 单页
docker-compose.yml
```
