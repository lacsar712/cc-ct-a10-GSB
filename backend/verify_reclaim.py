"""端到端验证占位回收：快照不追溯、CAS 写结论、并发竞态唯一结局、账实对账。

直接对真实 PostgreSQL 跑。
用法（在 backend/ 下）：
    POSTGRES_HOST=/tmp/pgdata_cnc python3 verify_reclaim.py
"""

import datetime as dt
import os
import sys
import threading
import time

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.db import close_old_connections  # noqa: E402
from django.utils import timezone  # noqa: E402

from desk.models import DeskSetting, OffsetSubmission, ReclaimRecord  # noqa: E402
from desk import services  # noqa: E402

PASS_, FAIL_ = 0, 0


def check(name, cond, detail=""):
    global PASS_, FAIL_
    if cond:
        PASS_ += 1
        print(f"  [PASS] {name}")
    else:
        FAIL_ += 1
        print(f"  [FAIL] {name}  {detail}")


def reset():
    ReclaimRecord.objects.all().delete()
    OffsetSubmission.objects.all().delete()
    DeskSetting.objects.all().delete()


def set_limit(seconds):
    setting = DeskSetting.get_solo()
    setting.hold_limit_seconds = seconds
    setting.save()
    return setting


def new_pending(tool="T99", offset=5):
    return OffsetSubmission.objects.create(
        tool_code=tool, offset_um=offset, status=OffsetSubmission.Status.PENDING
    )


def backdate(submission_id, seconds):
    OffsetSubmission.objects.filter(pk=submission_id).update(
        claimed_at=timezone.now() - dt.timedelta(seconds=seconds)
    )


# ------------------------------------------------- 1. 快照 + 不追溯


def test_snapshot_nonretroactive():
    print("\n[1] 认领快照与改时长不追溯")
    reset()
    set_limit(60)

    s1 = new_pending("T01")
    c1 = services.claim_next_pending()
    check("领到 s1", c1 and c1.id == s1.id)
    check("s1 时限快照=60", c1.claim_limit_seconds == 60, c1.claim_limit_seconds)
    check("s1 复核中且有占位起时",
          c1.status == OffsetSubmission.Status.PROCESSING and c1.claimed_at)

    # 改短到 2 秒：不追溯已占的 s1（仍按 60 秒）。
    set_limit(2)
    s1.refresh_from_db()
    check("改时长不追溯 s1（仍 60）", s1.claim_limit_seconds == 60,
          s1.claim_limit_seconds)

    recs = services.reclaim_expired()
    s1.refresh_from_db()
    check("未超时不回收 s1",
          s1.status == OffsetSubmission.Status.PROCESSING and recs == [],
          f"status={s1.status} recs={len(recs)}")

    # 新认领 s2 用新时限 2 秒。
    new_pending("T02")
    c2 = services.claim_next_pending()
    check("s2 时限快照=2（新单按新时长）", c2.claim_limit_seconds == 2,
          c2.claim_limit_seconds)


# ------------------------------------------------- 2. 超时回收 + 账 + 对账


def test_reclaim_and_ledger():
    print("\n[2] 超时吐回 + 回收账 + 对账")
    reset()
    set_limit(2)

    s = new_pending("T03")
    c = services.claim_next_pending()
    backdate(c.pk, 5)  # 占位起点倒拨 5 秒，制造超时

    recs = services.reclaim_expired()
    s.refresh_from_db()
    check("超时单吐回待复核", s.status == OffsetSubmission.Status.PENDING)
    check("吐回后清空占位字段",
          s.claimed_at is None and s.claim_limit_seconds is None)
    check("回收计数 +1", s.reclaim_count == 1, s.reclaim_count)
    check("回收账 +1", len(recs) == 1 and ReclaimRecord.objects.count() == 1)
    rec = recs[0]
    check("账单据对应正确",
          rec.submission_id == s.id and rec.limit_seconds == 2
          and rec.tool_code == "T03")
    check("占用时长 >= 时限", rec.held_seconds >= 2, rec.held_seconds)
    check("对账 ok", services.reconcile()["ok"], services.reconcile()["mismatches"])

    # 幂等：再扫一次不重复记账。
    again = services.reclaim_expired()
    s.refresh_from_db()
    check("重复扫描不重复记账",
          again == [] and s.reclaim_count == 1
          and ReclaimRecord.objects.count() == 1)


# ------------------------------------------------- 3. CAS 写结论 vs 回收 竞态


def run_in_thread(fn, errors):
    def wrapper():
        try:
            close_old_connections()
            fn()
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))
        finally:
            close_old_connections()
    t = threading.Thread(target=wrapper)
    t.start()
    return t


def test_race_finalize_vs_reclaim():
    print("\n[3] 并发竞态：写结论 vs 回收，只许一种结局（多轮）")
    wins = {"finalize": 0, "reclaim": 0, "neither": 0, "both": 0}
    rounds = 20
    for i in range(rounds):
        reset()
        set_limit(1)

        new_pending(f"T{i}")
        c = services.claim_next_pending()
        sid, claimed_at = c.id, c.claimed_at

        # 等到刚好越过 deadline：此刻 finalize（状态仍 processing）与
        # reclaim（已超时）同时具备资格，二者真抢同一行锁。
        sleep_for = 1.0 - (timezone.now() - claimed_at).total_seconds() + 0.03
        time.sleep(max(sleep_for, 0))

        errors = []
        barrier = threading.Barrier(2)

        def do_finalize():
            barrier.wait()
            services.finalize_submission(sid, claimed_at)

        def do_reclaim():
            barrier.wait()
            services.reclaim_expired()

        t1 = run_in_thread(do_finalize, errors)
        t2 = run_in_thread(do_reclaim, errors)
        t1.join()
        t2.join()
        check(f"轮{i} 无线程异常", not errors, errors)

        c.refresh_from_db()
        ledger_n = ReclaimRecord.objects.filter(submission_id=sid).count()
        finalized = c.status == OffsetSubmission.Status.DONE and bool(c.verdict)
        reclaimed = (
            c.status == OffsetSubmission.Status.PENDING and c.reclaim_count == 1
        )

        if finalized and not reclaimed and ledger_n == 0:
            wins["finalize"] += 1
        elif reclaimed and not finalized and ledger_n == 1:
            wins["reclaim"] += 1
        elif finalized and reclaimed:
            wins["both"] += 1
        else:
            wins["neither"] += 1

        check(f"轮{i} 结局唯一（非 both/neither）",
              (finalized and not reclaimed) or (reclaimed and not finalized),
              f"status={c.status} verdict={c.verdict} count={c.reclaim_count} "
              f"ledger={ledger_n}")
        check(f"轮{i} 无 processing 半截状态",
              c.status != OffsetSubmission.Status.PROCESSING
              and c.claimed_at is None)
        check(f"轮{i} 账实一致",
              services.reconcile()["ok"], services.reconcile()["mismatches"])

    print(f"  结局分布: {wins}")
    check("两种结局都出现过（证明是真互斥而非一边倒）",
          wins["finalize"] > 0 and wins["reclaim"] > 0, wins)
    check("从无 both / neither",
          wins["both"] == 0 and wins["neither"] == 0, wins)


# ------------------------------------------------- 4. 同单多次回收（占位时刻不同）


def test_reclaim_then_reclaim_again():
    print("\n[4] 同一单两次超时回收，记两笔账（占位时刻不同）")
    reset()
    set_limit(1)

    s = new_pending("T10")
    for n in (1, 2):
        c = services.claim_next_pending()
        backdate(c.pk, 2)
        services.reclaim_expired()
        s.refresh_from_db()
        check(f"第{n}次回收后计数={n}", s.reclaim_count == n, s.reclaim_count)

    check("回收账共 2 笔",
          ReclaimRecord.objects.filter(submission_id=s.id).count() == 2)
    check("对账 ok",
          services.reconcile()["ok"], services.reconcile()["mismatches"])


if __name__ == "__main__":
    test_snapshot_nonretroactive()
    test_reclaim_and_ledger()
    test_race_finalize_vs_reclaim()
    test_reclaim_then_reclaim_again()
    print(f"\n==== {PASS_} passed, {FAIL_} failed ====")
    sys.exit(1 if FAIL_ else 0)
