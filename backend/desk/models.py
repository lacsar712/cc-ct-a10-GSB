from django.contrib.auth.models import AbstractUser
from django.db import models


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


class HoldConfig(models.Model):
    """最长占位时长设置（单行）。改时长只影响之后新进入复核中的单。"""

    max_hold_seconds = models.PositiveIntegerField(default=300, verbose_name="最长占位秒数")
    updated_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="hold_config_updates",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "占位时长设置"

    def __str__(self) -> str:
        return f"最长占位 {self.max_hold_seconds} 秒"

    @classmethod
    def get(cls) -> "HoldConfig":
        obj = cls.objects.first()
        if obj is None:
            obj = cls.objects.create(max_hold_seconds=300)
        return obj


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

    # ---- 占位（复核中）相关 ----
    claimed_at = models.DateTimeField(null=True, blank=True, verbose_name="占入时间")
    hold_seconds = models.PositiveIntegerField(
        null=True, blank=True, verbose_name="本单占位时限快照（秒）"
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.tool_code} {self.offset_um}µm"


class ReclamationRecord(models.Model):
    """回收账：每次占位超时被系统吐回候审都落一条，可与真实状态对账。"""

    class Reason(models.TextChoices):
        TIMEOUT = "timeout", "占位超时"

    submission = models.ForeignKey(
        OffsetSubmission,
        on_delete=models.CASCADE,
        related_name="reclamations",
    )
    tool_code = models.CharField(max_length=32)
    claimed_at = models.DateTimeField(verbose_name="占入时间")
    hold_seconds = models.PositiveIntegerField(verbose_name="当时适用的占位秒数")
    released_at = models.DateTimeField(db_index=True, verbose_name="吐回候审时间")
    reason = models.CharField(
        max_length=16,
        choices=Reason.choices,
        default=Reason.TIMEOUT,
    )

    class Meta:
        ordering = ["-released_at"]

    def __str__(self) -> str:
        return f"回收 {self.tool_code}（{self.hold_seconds}s）"
