"""占位回收领域服务。

关键约定：
- 单子进入「复核中」时把当时的最长占位秒数快照到该行（hold_seconds），
  之后修改时长只影响新进入复核中的单，已占着的单不追溯。
- 写结论与超时回收都在单行事务内做「条件更新」（WHERE status='processing'），
  行锁串行化两者，因此同一时刻只允许一种结局：
  写结论成功 -> 状态 done，回收条件更新命中 0 行，不记账；
  回收成功   -> 状态回 pending 且记回收账，写结论方查到 0 行直接放弃，
               不会留下「复核中 + 半截结论」之类的中间状态。
"""

from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from desk.models import HoldConfig, OffsetSubmission, ReclamationRecord

MIN_HOLD_SECONDS = 1
MAX_HOLD_SECONDS = 7 * 24 * 3600  # 一周，仅用于挡住误填的离谱值


class StaleHoldError(RuntimeError):
    """写结论时单子已不在复核中（已被超时回收）。"""


def evaluate_verdict(offset_um: int) -> str:
    if abs(offset_um) <= settings.OFFSET_TOLERANCE_UM:
        return OffsetSubmission.Verdict.PASS
    return OffsetSubmission.Verdict.FAIL


# ---------- 时长设置 ----------

def get_hold_config() -> HoldConfig:
    return HoldConfig.get()


def update_max_hold_seconds(seconds: int, updated_by=None) -> HoldConfig:
    """更新最长占位时长。只作用于之后新进入复核中的单。"""
    if not isinstance(seconds, int) or isinstance(seconds, bool):
        raise ValueError("占位时长必须是整数秒")
    if seconds < MIN_HOLD_SECONDS or seconds > MAX_HOLD_SECONDS:
        raise ValueError(f"占位时长需在 {MIN_HOLD_SECONDS}～{MAX_HOLD_SECONDS} 秒之间")
    with transaction.atomic():
        config = HoldConfig.objects.select_for_update().first()
        if config is None:
            config = HoldConfig(max_hold_seconds=seconds)
        else:
            config.max_hold_seconds = seconds
        config.updated_by = updated_by
        config.save()
        return config


# ---------- 认领（占入复核中，快照时长） ----------

def claim_one_pending(now=None) -> Optional[OffsetSubmission]:
    """锁一条待复核单，置为「复核中」并快照当前占位时长。

    用 select_for_update(skip_locked) 跳过被别人占住的行。
    无单可领返回 None。时长在持锁期间读取，保证本次占位用的就是当下设置。
    """
    now = now or timezone.now()
    with transaction.atomic():
        submission = (
            OffsetSubmission.objects.select_for_update(skip_locked=True)
            .filter(status=OffsetSubmission.Status.PENDING)
            .order_by("created_at", "id")
            .first()
        )
        if submission is None:
            return None
        hold_seconds = get_hold_config().max_hold_seconds
        mark_processing(submission, hold_seconds, now)
        return submission


def mark_processing(submission: OffsetSubmission, hold_seconds: int, now=None) -> None:
    now = now or timezone.now()
    submission.status = OffsetSubmission.Status.PROCESSING
    submission.verdict = ""
    submission.reviewed_at = None
    submission.claimed_at = now
    submission.hold_seconds = hold_seconds
    submission.save(
        update_fields=[
            "status",
            "verdict",
            "reviewed_at",
            "claimed_at",
            "hold_seconds",
        ]
    )


def simulate_hold(submission_id: int, now=None) -> OffsetSubmission:
    """把一条候审单置为「复核中」占住但不写结论（演示/验收用）。

    模拟写权限员占单后迟迟不出结论：正常认领只捞 pending，不会再碰它，
    只有 reclaim_expired 到点把它吐回候审。仅当单子当前为候审时允许。
    """
    now = now or timezone.now()
    with transaction.atomic():
        submission = (
            OffsetSubmission.objects.select_for_update()
            .filter(pk=submission_id, status=OffsetSubmission.Status.PENDING)
            .first()
        )
        if submission is None:
            raise StaleHoldError("只有待复核的单才能占位；该单当前不可占")
        hold_seconds = get_hold_config().max_hold_seconds
        mark_processing(submission, hold_seconds, now)
        return submission


def simulate_create_hold(tool_code: str, offset_um: int, submitted_by=None, now=None) -> OffsetSubmission:
    """直接造一条「一进来就被占住、不写结论」的复核中单（演示/验收用）。

    原子地以 PROCESSING 落库并快照当前时长，不经过候审队列，
    因此不会被正常认领抢走，到点必由回收线程吐回候审。
    """
    now = now or timezone.now()
    with transaction.atomic():
        hold_seconds = get_hold_config().max_hold_seconds
        return OffsetSubmission.objects.create(
            tool_code=tool_code,
            offset_um=offset_um,
            submitted_by=submitted_by,
            status=OffsetSubmission.Status.PROCESSING,
            verdict="",
            claimed_at=now,
            hold_seconds=hold_seconds,
        )


# ---------- 写结论（与回收互斥的条件更新） ----------

def complete_submission(submission_id: int, now=None) -> OffsetSubmission:
    """对复核中的单写结论。

    持行锁后复核 status 仍为 processing 才落结论；
    若已被回收（pending）则抛 StaleHoldError，不写任何字段。
    """
    now = now or timezone.now()
    with transaction.atomic():
        submission = (
            OffsetSubmission.objects.select_for_update()
            .filter(pk=submission_id, status=OffsetSubmission.Status.PROCESSING)
            .first()
        )
        if submission is None:
            raise StaleHoldError(f"单子 {submission_id} 已不在复核中，可能已被超时回收")

        submission.verdict = evaluate_verdict(submission.offset_um)
        submission.status = OffsetSubmission.Status.DONE
        submission.reviewed_at = now
        submission.claimed_at = None
        submission.hold_seconds = None
        submission.save(
            update_fields=[
                "verdict",
                "status",
                "reviewed_at",
                "claimed_at",
                "hold_seconds",
            ]
        )
        return submission


# ---------- 超时回收 ----------

def _is_expired(row: OffsetSubmission, now) -> bool:
    if row.claimed_at is None or row.hold_seconds is None:
        return False
    return now - row.claimed_at >= timedelta(seconds=row.hold_seconds)


def reclaim_expired(now=None, limit: int = 100) -> list[ReclamationRecord]:
    """扫描复核中占位，把超时的单吐回待复核并逐条记回收账。

    两阶段：先无锁读出候选 id，再只锁候选行做条件更新，
    避免长时间锁住全部复核中行。返回本次新落的回收账列表。
    """
    now = now or timezone.now()
    candidates = list(
        OffsetSubmission.objects.filter(
            status=OffsetSubmission.Status.PROCESSING,
            claimed_at__isnull=False,
            hold_seconds__isnull=False,
        ).values("id", "claimed_at", "hold_seconds")[:2000]
    )
    expired_ids = [
        c["id"]
        for c in candidates
        if now - c["claimed_at"] >= timedelta(seconds=c["hold_seconds"])
    ]
    if not expired_ids:
        return []

    records: list[ReclamationRecord] = []
    with transaction.atomic():
        rows = list(
            OffsetSubmission.objects.select_for_update(skip_locked=True)
            .filter(id__in=expired_ids)
            .order_by("claimed_at", "id")[:limit]
        )
        for row in rows:
            # 锁内再次按时钟确认，防止临界误收
            if not _is_expired(row, now):
                continue
            updated = (
                OffsetSubmission.objects.filter(
                    pk=row.pk, status=OffsetSubmission.Status.PROCESSING
                ).update(
                    status=OffsetSubmission.Status.PENDING,
                    claimed_at=None,
                    hold_seconds=None,
                )
            )
            # updated==0：并发的写结论已先行提交为 done，本次不回收、不记账。
            if not updated:
                continue
            records.append(
                ReclamationRecord.objects.create(
                    submission_id=row.pk,
                    tool_code=row.tool_code,
                    claimed_at=row.claimed_at,
                    hold_seconds=row.hold_seconds,
                    released_at=now,
                    reason=ReclamationRecord.Reason.TIMEOUT,
                )
            )
    return records


# ---------- 占位监视与对账 ----------

def hold_watchlist(now=None) -> list[dict]:
    """占位监视列表：每条复核中的单与其快照时限、剩余秒数。"""
    now = now or timezone.now()
    rows = (
        OffsetSubmission.objects.filter(status=OffsetSubmission.Status.PROCESSING)
        .order_by("claimed_at", "id")
    )
    result = []
    for row in rows:
        remaining: Optional[int] = None
        expired = False
        if row.claimed_at is not None and row.hold_seconds is not None:
            deadline = row.claimed_at + timedelta(seconds=row.hold_seconds)
            remaining = max(0, int((deadline - now).total_seconds()))
            expired = now >= deadline
        result.append(
            {
                "submission_id": row.id,
                "tool_code": row.tool_code,
                "offset_um": row.offset_um,
                "claimed_at": row.claimed_at,
                "hold_seconds": row.hold_seconds,
                "remaining_seconds": remaining,
                "expired": expired,
            }
        )
    return result


def reclamation_ledger(limit: int = 200) -> list[ReclamationRecord]:
    return list(ReclamationRecord.objects.select_related("submission")[:limit])


def reconcile(now=None) -> dict:
    """占位监视列表、回收账与真实状态三方对账。

    返回 consistent=True 表示无破损；issues 列出每一条对不上的账。
    """
    now = now or timezone.now()
    issues: list[dict] = []

    processing = list(
        OffsetSubmission.objects.filter(status=OffsetSubmission.Status.PROCESSING)
    )

    # 1) 真实状态在复核中，却没有占位快照 —— 监视列表看不到它，属于脱管占位。
    for row in processing:
        if row.claimed_at is None or row.hold_seconds is None:
            issues.append(
                {
                    "kind": "processing_without_claim",
                    "submission_id": row.id,
                    "tool_code": row.tool_code,
                    "message": "复核中但缺少占入时间/时限快照，不在占位监视内",
                }
            )
        elif row.verdict:
            # 复核中不应残留结论（写结论是 status+verdict 同事务落库）。
            issues.append(
                {
                    "kind": "processing_with_verdict",
                    "submission_id": row.id,
                    "tool_code": row.tool_code,
                    "message": "复核中却残留结论，属于半截状态",
                }
            )

    # 2) 已离开复核中（候审/已完成）却残留占位快照，也是半截状态。
    leftovers = (
        OffsetSubmission.objects.exclude(status=OffsetSubmission.Status.PROCESSING)
        .filter(Q(claimed_at__isnull=False) | Q(hold_seconds__isnull=False))
        .values("id", "tool_code")
    )
    for row in leftovers:
        issues.append(
            {
                "kind": "leftover_claim_snapshot",
                "submission_id": row["id"],
                "tool_code": row["tool_code"],
                "message": "已离开复核中却残留占入时间/时限快照",
            }
        )

    # 3) 回收账与真实状态对账：账记了吐回候审，该单就不得停在同一次占位的复核中。
    # 只查当前仍在复核中的单对应的账，避免回收账全表扫描。
    current = {r.id: r for r in processing}
    ledger_total = ReclamationRecord.objects.count()
    relevant_ledger = ReclamationRecord.objects.filter(submission_id__in=list(current.keys()))
    for rec in relevant_ledger:
        row = current.get(rec.submission_id)
        if row is not None and row.claimed_at is not None:
            # 当前仍在同一次占位里（占入时间未更新为回收之后的新占位）= 账实不符。
            if row.claimed_at <= rec.released_at:
                issues.append(
                    {
                        "kind": "reclaimed_but_processing",
                        "submission_id": row.id,
                        "tool_code": row.tool_code,
                        "reclamation_id": rec.id,
                        "message": "回收账已记吐回候审，但单子仍卡在同一次复核中",
                    }
                )

    # 4) 已超时但尚未被下一轮回收扫到的单：属于正常的轮询间隙，单独暴露不计为破损。
    overdue = [
        row.id
        for row in processing
        if row.claimed_at is not None
        and row.hold_seconds is not None
        and _is_expired(row, now)
    ]

    return {
        "consistent": not issues,
        "issues": issues,
        "processing_count": len(processing),
        "ledger_count": ledger_total,
        "overdue_pending_reclaim_ids": overdue,
        "checked_at": now,
    }
