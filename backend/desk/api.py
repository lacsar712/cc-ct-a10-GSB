from datetime import datetime
from typing import Optional

from django.http import HttpRequest
from ninja import NinjaAPI, Schema
from ninja.errors import HttpError

from desk.auth_utils import bearer_auth, create_access_token, verify_password
from desk.models import OffsetSubmission, ReclamationRecord, User
from desk import services
from desk.services import StaleHoldError

api = NinjaAPI(title="数控刀补复核台", version="1.1")


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
    hold_seconds: Optional[int]


class HoldConfigOut(Schema):
    max_hold_seconds: int
    updated_at: Optional[datetime]
    updated_by: Optional[str]


class HoldConfigIn(Schema):
    max_hold_seconds: int


class SimulateHoldIn(Schema):
    tool_code: str
    offset_um: int


class HoldOut(Schema):
    submission_id: int
    tool_code: str
    offset_um: int
    claimed_at: Optional[datetime]
    hold_seconds: Optional[int]
    remaining_seconds: Optional[int]
    expired: bool


class ReclamationOut(Schema):
    id: int
    submission_id: int
    tool_code: str
    claimed_at: datetime
    hold_seconds: int
    released_at: datetime
    reason: str


class ReconcileIssue(Schema):
    kind: str
    submission_id: Optional[int] = None
    tool_code: Optional[str] = None
    reclamation_id: Optional[int] = None
    message: str


class ReconcileOut(Schema):
    consistent: bool
    issues: list[ReconcileIssue]
    processing_count: int
    ledger_count: int
    overdue_pending_reclaim_ids: list[int]
    checked_at: datetime


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
        hold_seconds=row.hold_seconds,
    )


def _require_write(user: User) -> None:
    if not user.can_write:
        raise HttpError(403, "当前账号只读，不能修改占位时长设置")


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


@api.post("/submissions/{submission_id}/hold", response=SubmissionOut, auth=bearer_auth)
def hold_submission(request: HttpRequest, submission_id: int):
    """模拟「写权限员占住复核但迟迟不写结论」，用于占位超时回收验收。"""
    user: User = request.auth
    _require_write(user)
    try:
        row = services.simulate_hold(submission_id)
    except StaleHoldError as exc:
        raise HttpError(409, str(exc))
    return _to_out(row)


# ---------- 占位回收台 ----------

@api.get("/hold-config", response=HoldConfigOut, auth=bearer_auth)
def get_hold_config(request: HttpRequest):
    cfg = services.get_hold_config()
    return {
        "max_hold_seconds": cfg.max_hold_seconds,
        "updated_at": cfg.updated_at,
        "updated_by": cfg.updated_by.username if cfg.updated_by else None,
    }


@api.put("/hold-config", response=HoldConfigOut, auth=bearer_auth)
def update_hold_config(request: HttpRequest, body: HoldConfigIn):
    user: User = request.auth
    _require_write(user)
    try:
        cfg = services.update_max_hold_seconds(body.max_hold_seconds, updated_by=user)
    except ValueError as exc:
        raise HttpError(400, str(exc))
    return {
        "max_hold_seconds": cfg.max_hold_seconds,
        "updated_at": cfg.updated_at,
        "updated_by": cfg.updated_by.username if cfg.updated_by else None,
    }


@api.get("/holds", response=list[HoldOut], auth=bearer_auth)
def list_holds(request: HttpRequest):
    return services.hold_watchlist()


@api.post("/holds/reclaim", response=list[ReclamationOut], auth=bearer_auth)
def reclaim_now(request: HttpRequest):
    """手动触发一轮超时回收（自动回收由 worker 周期执行）。仅写权限员。"""
    user: User = request.auth
    _require_write(user)
    records = services.reclaim_expired()
    return [_reclamation_out(r) for r in records]


@api.post("/holds/simulate", response=SubmissionOut, auth=bearer_auth)
def simulate_hold_api(request: HttpRequest, body: SimulateHoldIn):
    """造一条一进入复核中就被占住、不写结论的单（验收超时回收用）。仅写权限员。"""
    user: User = request.auth
    _require_write(user)
    tool_code = body.tool_code.strip()
    if not tool_code:
        raise HttpError(400, "刀具编号不能为空")
    row = services.simulate_create_hold(tool_code, body.offset_um, submitted_by=user)
    return _to_out(row)


@api.get("/holds/reconcile", response=ReconcileOut, auth=bearer_auth)
def reconcile(request: HttpRequest):
    return services.reconcile()


@api.get("/reclamations", response=list[ReclamationOut], auth=bearer_auth)
def list_reclamations(request: HttpRequest):
    return [_reclamation_out(r) for r in services.reclamation_ledger()]


def _reclamation_out(r: ReclamationRecord) -> ReclamationOut:
    return ReclamationOut(
        id=r.id,
        submission_id=r.submission_id,
        tool_code=r.tool_code,
        claimed_at=r.claimed_at,
        hold_seconds=r.hold_seconds,
        released_at=r.released_at,
        reason=r.reason,
    )
