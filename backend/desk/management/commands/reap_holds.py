from django.core.management.base import BaseCommand

from desk.services import reclaim_expired


class Command(BaseCommand):
    help = "扫描复核中占位，把超过单内快照时限的单吐回待复核并记回收账"

    def handle(self, *args, **options):
        records = reclaim_expired()
        if not records:
            self.stdout.write("没有需要回收的超时占位")
            return
        for rec in records:
            self.stdout.write(
                f"回收 #{rec.submission_id} {rec.tool_code}：占入 {rec.claimed_at:%Y-%m-%d %H:%M:%S}"
                f" / 时限 {rec.hold_seconds}s / 吐回 {rec.released_at:%H:%M:%S}（账 #{rec.id}）"
            )
        self.stdout.write(self.style.SUCCESS(f"共回收 {len(records)} 条"))
