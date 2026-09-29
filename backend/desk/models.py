from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


class User(AbstractUser):
    class Role(models.TextChoices):
        MACHINIST = "machinist", "操作员"
        AUDITOR = "auditor", "复核员"

    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.MACHINIST,
    )

    @property
    def can_write(self) -> bool:
        return self.role == self.Role.MACHINIST


class OffsetSubmission(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "待复核"
        PROCESSING = "processing", "复核中"
        DONE = "done", "已完成"

    class Verdict(models.TextChoices):
        PASS = "合格", "合格"
        FAIL = "超差", "超差"

    tool_code = models.CharField(max_length=32, db_index=True)
    offset_um = models.IntegerField()
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    verdict = models.CharField(
        max_length=8,
        choices=Verdict.choices,
        blank=True,
        default="",
    )
    submitted_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="submissions",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    # ---- 占位（复核中）状态 ----
    # 进入「复核中」的时刻；不变量：status=processing ⟺ claimed_at 非空。
    claimed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # 认领那一刻快照下来的最长占位秒数；改时限只影响此后新认领的单，不追溯已占单。
    claim_limit_seconds = models.PositiveIntegerField(null=True, blank=True)
    # 被超时回收（吐回待复核）的累计次数。
    reclaim_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.tool_code} {self.offset_um}µm"


class DeskSetting(models.Model):
    """单行（id 恒为 1）全局设置：复核中最长占位秒数。"""

    DEFAULT_HOLD_LIMIT_SECONDS = 60
    MIN_HOLD_LIMIT_SECONDS = 1
    MAX_HOLD_LIMIT_SECONDS = 86_400

    id = models.SmallIntegerField(primary_key=True, editable=False, default=1)
    hold_limit_seconds = models.PositiveIntegerField(default=DEFAULT_HOLD_LIMIT_SECONDS)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="setting_updates",
    )

    class Meta:
        verbose_name = "复核台设置"

    @classmethod
    def get_solo(cls) -> "DeskSetting":
        obj, _ = cls.objects.get_or_create(
            id=1,
            defaults={"hold_limit_seconds": cls.DEFAULT_HOLD_LIMIT_SECONDS},
        )
        return obj

    def __str__(self) -> str:
        return f"最长占位 {self.hold_limit_seconds} 秒"


class ReclaimRecord(models.Model):
    """回收账：每次「复核中超时被吐回待复核」记一条，可与真实状态对账。"""

    submission = models.ForeignKey(
        OffsetSubmission,
        on_delete=models.CASCADE,
        related_name="reclaims",
    )
    tool_code = models.CharField(max_length=32)
    # 本次被回收占位的起始时刻（主表回收后会清空 claimed_at，故在此留底）。
    claimed_at = models.DateTimeField()
    # 该次占位认领时生效的时限与实际占用时长（秒）。
    limit_seconds = models.PositiveIntegerField()
    held_seconds = models.FloatField()
    reclaimed_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-reclaimed_at", "-id"]
        constraints = [
            # 同一次占位（submission + claimed_at）至多一条账：从存储层杜绝重复回收记账。
            models.UniqueConstraint(
                fields=["submission", "claimed_at"],
                name="uniq_reclaim_per_hold",
            )
        ]

    def __str__(self) -> str:
        return f"{self.tool_code} 回收于 {self.reclaimed_at:%Y-%m-%d %H:%M:%S}"
