import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("desk", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="HoldConfig",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "max_hold_seconds",
                    models.PositiveIntegerField(default=300, verbose_name="最长占位秒数"),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="hold_config_updates",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "占位时长设置",
            },
        ),
        migrations.AddField(
            model_name="offsetsubmission",
            name="claimed_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="占入时间"),
        ),
        migrations.AddField(
            model_name="offsetsubmission",
            name="hold_seconds",
            field=models.PositiveIntegerField(
                blank=True, null=True, verbose_name="本单占位时限快照（秒）"
            ),
        ),
        migrations.CreateModel(
            name="ReclamationRecord",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("tool_code", models.CharField(max_length=32)),
                ("claimed_at", models.DateTimeField(verbose_name="占入时间")),
                ("hold_seconds", models.PositiveIntegerField(verbose_name="当时适用的占位秒数")),
                ("released_at", models.DateTimeField(db_index=True, verbose_name="吐回候审时间")),
                (
                    "reason",
                    models.CharField(
                        choices=[("timeout", "占位超时")],
                        default="timeout",
                        max_length=16,
                    ),
                ),
                (
                    "submission",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reclamations",
                        to="desk.offsetsubmission",
                    ),
                ),
            ],
            options={
                "ordering": ["-released_at"],
            },
        ),
    ]
