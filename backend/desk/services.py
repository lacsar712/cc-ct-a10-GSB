"""复核台核心服务：认领占位、写结论、超时回收与账实对账。

并发安全约定（PostgreSQL 行锁 + 条件更新）：

* 认领（claim）在同一事务里用 ``select_for_update(skip_locked=True)`` 取一条
  待复核单，置为「复核中」并快照占位起始时刻与当时生效的最长占位时长。
* 写结论（finalize）与超时回收（reclaim）都先锁行再复查状态：
  两者在数据库层被串行化，先提交者赢，后到者的条件复查失败、什么都不改，
  因此「写结论成功」与「回收成功」对同一次占位只有一种结局，
  也不会留下「复核中」的半截状态或重复回收账。
* 不变量：``status=processing`` ⟺ ``claimed_at`` 非空。
"""

from datetime import timedelta
from typing import Optional

from django.db import transaction
from django.db.models import Count, F
from django.utils import timezone

from desk.models import DeskSetting, OffsetSubmission, ReclaimRecord


def evaluate_verdict(offset_um: int) -> str:
    from django.conf import settings

    if abs(offset_um) <= settings.OFFSET_TOLERANCE_UM:
        return OffsetSubmission.Verdict.PASS
    return OffsetSubmission.Verdict.FAIL


# ---------------------------------------------------------------- 认领占位


def claim_next_pending() -> Optional[OffsetSubmission]:
    """认领一条待复核单：置「复核中」并快照时限。无单可领返回 None。"""
    with transaction.atomic():
        submission = (
            OffsetSubmission.objects.select_for_update(skip_locked=True)
            .filter(status=OffsetSubmission.Status.PENDING)
            .order_by("created_at", "id")
            .first()
        )
        if submission is None:
            return None

        limit = DeskSetting.get_solo().hold_limit_seconds
        now = timezone.now()
        submission.status = OffsetSubmission.Status.PROCESSING
        submission.claimed_at = now
        submission.claim_limit_seconds = limit
        submission.save(update_fields=["status", "claimed_at", "claim_limit_seconds"])
        # 取出锁内最终值，供调用方之后做 CAS 写结论。
        submission.refresh_from_db(
            fields=["status", "claimed_at", "claim_limit_seconds"]
        )
    return submission


# ---------------------------------------------------------------- 写结论


def finalize_submission(submission_id: int, claimed_at) -> bool:
    """对一次占位写结论。仅当单子仍处于「同一次复核中占位」时成功。

    返回 True 表示写结论成功；False 表示该占位已被回收（或单子已不在复核中），
    本次结论作废，不会改动任何数据。
    """
    with transaction.atomic():
        submission = (
            OffsetSubmission.objects.select_for_update()
            .filter(pk=submission_id)
            .first()
        )
        if submission is None:
            return False
        # 行锁等待后复查：必须仍是同一次占位（claimed_at 一致）才能写结论。
        if (
            submission.status != OffsetSubmission.Status.PROCESSING
            or submission.claimed_at is None
            or submission.claimed_at != claimed_at
        ):
            return False

        submission.verdict = evaluate_verdict(submission.offset_um)
        submission.status = OffsetSubmission.Status.DONE
        submission.reviewed_at = timezone.now()
        submission.claimed_at = None
        submission.claim_limit_seconds = None
        submission.save(
            update_fields=[
                "verdict",
                "status",
                "reviewed_at",
                "claimed_at",
                "claim_limit_seconds",
            ]
        )
    return True


# ---------------------------------------------------------------- 超时回收


def reclaim_expired(now=None) -> list[ReclaimRecord]:
    """把所有超过快照时限仍未写结论的「复核中」单吐回待复核并记回收账。

    返回本次新产生的回收账列表。
    """
    if now is None:
        now = timezone.now()

    # 先用不加锁的快照圈出候选（deadline 是行内计算字段），再逐条锁行复查。
    candidates = list(
        OffsetSubmission.objects.filter(
            status=OffsetSubmission.Status.PROCESSING,
            claimed_at__isnull=False,
        ).only("id", "claimed_at", "claim_limit_seconds")
    )

    reclaimed: list[ReclaimRecord] = []
    for candidate in candidates:
        record = _reclaim_one(candidate.id, now)
        if record is not None:
            reclaimed.append(record)
    return reclaimed


def _reclaim_one(submission_id: int, now) -> Optional[ReclaimRecord]:
    with transaction.atomic():
        submission = (
            OffsetSubmission.objects.select_for_update()
            .filter(pk=submission_id, status=OffsetSubmission.Status.PROCESSING)
            .first()
        )
        if submission is None or submission.claimed_at is None:
            # 锁等待期间已被写结论，或占位已解除：不回收。
            return None

        limit = submission.claim_limit_seconds or 0
        deadline = submission.claimed_at + timedelta(seconds=limit)
        if now < deadline:
            # 按这单认领时的快照时限尚未超时（改长/改短都不追溯）。
            return None

        claimed_at = submission.claimed_at
        held_seconds = (now - claimed_at).total_seconds()

        submission.status = OffsetSubmission.Status.PENDING
        submission.claimed_at = None
        submission.claim_limit_seconds = None
        submission.reclaim_count = F("reclaim_count") + 1
        submission.save(
            update_fields=[
                "status",
                "claimed_at",
                "claim_limit_seconds",
                "reclaim_count",
            ]
        )

        # 与状态翻转同一事务提交；(submission, claimed_at) 唯一约束兜底，
        # 同一次占位不可能重复记账，账与状态要么同时生效要么同时回滚。
        record = ReclaimRecord.objects.create(
            submission=submission,
            tool_code=submission.tool_code,
            claimed_at=claimed_at,
            limit_seconds=limit,
            held_seconds=max(held_seconds, 0.0),
        )
    return record


# ---------------------------------------------------------------- 账实对账


def reconcile() -> dict:
    """对账：占位（真实状态）、回收账、回收计数三者必须一致。"""
    mismatches: list[str] = []

    processing_qs = OffsetSubmission.objects.filter(
        status=OffsetSubmission.Status.PROCESSING
    )
    processing_count = processing_qs.count()

    # 1) 不变量：复核中必有 claimed_at；非复核中必无 claimed_at。
    broken_processing = processing_qs.filter(claimed_at__isnull=True).count()
    if broken_processing:
        mismatches.append(f"{broken_processing} 条复核中单据缺少占位起始时刻")
    stray = OffsetSubmission.objects.exclude(
        status=OffsetSubmission.Status.PROCESSING
    ).filter(claimed_at__isnull=False).count()
    if stray:
        mismatches.append(f"{stray} 条非复核中单据残留占位起始时刻")

    # 2) 回收账里的任一次占位，都不应仍是单子当前的「复核中」占位
    #    （记了账说明已吐回，不能还卡在同一次复核中）。
    live_holds = {
        (row.id, row.claimed_at): None
        for row in processing_qs.only("id", "claimed_at")
    }
    ledger = list(
        ReclaimRecord.objects.values(
            "id", "submission_id", "claimed_at"
        )
    )
    for rec in ledger:
        if (rec["submission_id"], rec["claimed_at"]) in live_holds:
            mismatches.append(
                f"回收账 #{rec['id']} 对应的占位仍处于复核中（账已记、状态未吐回）"
            )

    # 3) 每条单的 reclaim_count 必须等于其回收账条数。
    count_rows = (
        OffsetSubmission.objects.annotate(ledger_n=Count("reclaims"))
        .exclude(reclaim_count=F("ledger_n"))
        .values_list("id", "reclaim_count", "ledger_n")
    )
    for sid, claimed_n, ledger_n in count_rows:
        mismatches.append(
            f"单据 #{sid} 回收次数 {claimed_n} 与回收账条数 {ledger_n} 不符"
        )

    return {
        "ok": not mismatches,
        "processing_count": processing_count,
        "ledger_count": len(ledger),
        "mismatches": mismatches,
    }
