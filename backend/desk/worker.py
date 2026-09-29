"""Background worker：认领写结论线程 + 超时回收线程。

- 主线程：claim_one_pending() 用 SKIP LOCKED 认领待复核单（快照当前占位时长），
  随后 complete_submission() 持行锁条件写结论；若此刻已被回收线程抢走结局
  （StaleHoldError）则放弃，单子保持待复核，绝不残留复核中半截状态。
- 回收线程：每 REAP_INTERVAL 秒跑一次 reclaim_expired()，把超过单内快照时限的
  占位吐回待复核并逐条记回收账。

两条线程各用独立数据库连接，写结论与回收在真实的行锁上互斥，
同一时刻只允许一种结局，见 desk/services.py。

环境变量：
- WORKER_HOLD_SECONDS：认领后故意占住多少秒不写结论（演示/验收用，默认 0）。
  设得比占位时长大，就能稳定观察到「占着不写结论 → 被回收」。
- REAP_INTERVAL_SECONDS：回收扫描间隔（默认 0.5 秒）。
"""

import os
import sys
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import django


def setup_django() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()


def process_one(hold_seconds_before_verdict: float = 0.0) -> bool:
    """认领一条待复核单并写结论。无单可领返回 False。"""
    from desk.services import (
        StaleHoldError,
        claim_one_pending,
        complete_submission,
    )

    submission = claim_one_pending()
    if submission is None:
        return False

    if hold_seconds_before_verdict:
        time.sleep(hold_seconds_before_verdict)

    try:
        complete_submission(submission.id)
    except StaleHoldError:
        print(
            f"submission {submission.id} was reclaimed before verdict; giving up this round",
            flush=True,
        )
    return True


def reap_once() -> None:
    from desk.services import reclaim_expired

    records = reclaim_expired()
    for rec in records:
        print(
            f"reclaim #{rec.submission_id} {rec.tool_code} held {rec.hold_seconds}s "
            f"-> pending (ledger #{rec.id})",
            flush=True,
        )


def reaper_loop(interval: float) -> None:
    from django.db import connection

    while True:
        try:
            reap_once()
        except Exception as exc:  # 数据库瞬断等不应杀死回收线程
            print(f"reaper error: {exc!r}", flush=True)
        finally:
            # 守护线程用独立连接，每拍主动关闭让 Django 下次重连
            connection.close()
        time.sleep(interval)


def run_loop(poll_seconds: float = 0.5, hold_seconds_before_verdict: float = 0.0) -> None:
    setup_django()
    reap_interval = float(os.environ.get("REAP_INTERVAL_SECONDS", "0.5") or "0.5")
    threading.Thread(
        target=reaper_loop,
        args=(reap_interval,),
        name="hold-reaper",
        daemon=True,
    ).start()
    print(
        f"cnc-offset worker started (hold-before-verdict={hold_seconds_before_verdict}s, "
        f"reap-interval={reap_interval}s)",
        flush=True,
    )
    while True:
        try:
            claimed = process_one(hold_seconds_before_verdict)
        except Exception as exc:  # 数据库瞬断等不应杀死 worker
            print(f"worker tick error: {exc!r}", flush=True)
            claimed = True
        if not claimed:
            time.sleep(poll_seconds)


if __name__ == "__main__":
    setup_django()
    hold_before = float(os.environ.get("WORKER_HOLD_SECONDS", "0") or "0")
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        process_one(hold_seconds_before_verdict=hold_before)
    elif len(sys.argv) > 1 and sys.argv[1] == "reap":
        reap_once()
    else:
        run_loop(hold_seconds_before_verdict=hold_before)
