from datetime import datetime
from typing import Optional

from django.http import HttpRequest
from django.utils import timezone
from ninja import NinjaAPI, Schema
from ninja.errors import HttpError

from desk.auth_utils import bearer_auth, create_access_token, verify_password
from desk.models import DeskSetting, OffsetSubmission, ReclaimRecord, User
from desk.services import reconcile

api = NinjaAPI(title="数控刀补复核台", version="1.0")


class HealthOut(Schema):
    status: str


class LoginIn(Schema):
    username: str
    password: str


class LoginOut(Schema):
    token: str
    username: str
    role: str
    can_write: bool


class SubmissionIn(Schema):
    tool_code: str
    offset_um: int


class SubmissionOut(Schema):
    id: int
    tool_code: str
    offset_um: int
    status: str
    verdict: str
    created_at: datetime
    reviewed_at: Optional[datetime]
    claimed_at: Optional[datetime]
    claim_limit_seconds: Optional[int]
    reclaim_count: int


def _to_out(row: OffsetSubmission) -> SubmissionOut:
    return SubmissionOut(
        id=row.id,
        tool_code=row.tool_code,
        offset_um=row.offset_um,
        status=row.status,
        verdict=row.verdict or "",
        created_at=row.created_at,
        reviewed_at=row.reviewed_at,
        claimed_at=row.claimed_at,
        claim_limit_seconds=row.claim_limit_seconds,
        reclaim_count=row.reclaim_count,
    )


# ---------------- 占位回收台 ----------------


class SettingOut(Schema):
    hold_limit_seconds: int
    updated_at: Optional[datetime]
    updated_by: Optional[str]


class HoldLimitIn(Schema):
    hold_limit_seconds: int


class WatchRow(Schema):
    id: int
    tool_code: str
    status: str
    claimed_at: datetime
    claim_limit_seconds: int
    held_seconds: float
    remaining_seconds: float
    expired: bool


class LedgerRow(Schema):
    id: int
    submission_id: int
    tool_code: str
    claimed_at: datetime
    limit_seconds: int
    held_seconds: float
    reclaimed_at: datetime


class ReconcileOut(Schema):
    ok: bool
    processing_count: int
    ledger_count: int
    mismatches: list[str]


class ReclaimOverviewOut(Schema):
    setting: SettingOut
    watching: list[WatchRow]
    ledger: list[LedgerRow]
    reconcile: ReconcileOut


def _setting_out(setting: DeskSetting) -> SettingOut:
    return SettingOut(
        hold_limit_seconds=setting.hold_limit_seconds,
        updated_at=setting.updated_at,
        updated_by=setting.updated_by.username if setting.updated_by_id else None,
    )


@api.get("/health", response=HealthOut)
def health(request: HttpRequest):
    return {"status": "ok"}


@api.post("/auth/login", response=LoginOut)
def login(request: HttpRequest, body: LoginIn):
    try:
        user = User.objects.get(username=body.username)
    except User.DoesNotExist:
        raise HttpError(401, "用户名或密码错误")
    if not verify_password(body.password, user.password):
        raise HttpError(401, "用户名或密码错误")
    token = create_access_token(user)
    return {
        "token": token,
        "username": user.username,
        "role": user.role,
        "can_write": user.can_write,
    }


@api.get("/submissions", response=list[SubmissionOut], auth=bearer_auth)
def list_submissions(request: HttpRequest):
    rows = OffsetSubmission.objects.all()[:200]
    return [_to_out(r) for r in rows]


@api.get("/submissions/{submission_id}", response=SubmissionOut, auth=bearer_auth)
def get_submission(request: HttpRequest, submission_id: int):
    try:
        row = OffsetSubmission.objects.get(pk=submission_id)
    except OffsetSubmission.DoesNotExist:
        raise HttpError(404, "刀补记录不存在")
    return _to_out(row)


@api.post("/submissions", response=SubmissionOut, auth=bearer_auth)
def create_submission(request: HttpRequest, body: SubmissionIn):
    user: User = request.auth
    if not user.can_write:
        raise HttpError(403, "当前账号只读，不能提交刀补")
    tool_code = body.tool_code.strip()
    if not tool_code:
        raise HttpError(400, "刀具编号不能为空")
    row = OffsetSubmission.objects.create(
        tool_code=tool_code,
        offset_um=body.offset_um,
        submitted_by=user,
        status=OffsetSubmission.Status.PENDING,
    )
    return _to_out(row)


@api.get("/reclaim/overview", response=ReclaimOverviewOut, auth=bearer_auth)
def reclaim_overview(request: HttpRequest):
    """占位回收台：时长设置 + 占位监视列表 + 回收账 + 账实对账。只读员也可查看。"""
    now = timezone.now()

    watching: list[WatchRow] = []
    for row in (
        OffsetSubmission.objects.filter(
            status=OffsetSubmission.Status.PROCESSING,
            claimed_at__isnull=False,
        ).order_by("claimed_at", "id")
    ):
        held = (now - row.claimed_at).total_seconds()
        limit = row.claim_limit_seconds or 0
        remaining = limit - held
        watching.append(
            WatchRow(
                id=row.id,
                tool_code=row.tool_code,
                status=row.status,
                claimed_at=row.claimed_at,
                claim_limit_seconds=limit,
                held_seconds=round(held, 1),
                remaining_seconds=round(remaining, 1),
                expired=remaining <= 0,
            )
        )

    ledger = [
        LedgerRow(
            id=r.id,
            submission_id=r.submission_id,
            tool_code=r.tool_code,
            claimed_at=r.claimed_at,
            limit_seconds=r.limit_seconds,
            held_seconds=round(r.held_seconds, 1),
            reclaimed_at=r.reclaimed_at,
        )
        for r in ReclaimRecord.objects.all()[:200]
    ]

    return ReclaimOverviewOut(
        setting=_setting_out(DeskSetting.get_solo()),
        watching=watching,
        ledger=ledger,
        reconcile=ReconcileOut(**reconcile()),
    )


@api.put("/reclaim/hold-limit", response=SettingOut, auth=bearer_auth)
def update_hold_limit(request: HttpRequest, body: HoldLimitIn):
    """修改最长占位秒数。仅写权限员（操作员）可改；只读员 403。

    只作用于此后新进入复核中的单，已占着的单按认领时的快照时限执行，不追溯。
    """
    user: User = request.auth
    if not user.can_write:
        raise HttpError(403, "当前账号只读，不能修改最长占位时长")
    seconds = body.hold_limit_seconds
    if not (
        DeskSetting.MIN_HOLD_LIMIT_SECONDS
        <= seconds
        <= DeskSetting.MAX_HOLD_LIMIT_SECONDS
    ):
        raise HttpError(
            400,
            f"最长占位时长需在 {DeskSetting.MIN_HOLD_LIMIT_SECONDS} 到 "
            f"{DeskSetting.MAX_HOLD_LIMIT_SECONDS} 秒之间",
        )
    setting = DeskSetting.get_solo()
    setting.hold_limit_seconds = seconds
    setting.updated_by = user
    setting.save(update_fields=["hold_limit_seconds", "updated_by", "updated_at"])
    return _setting_out(setting)
