"""后台进程：

* 认领进程（worker）：用 SKIP LOCKED 领取一条待复核单，置「复核中」并快照时限，
  占位 HOLD 秒（模拟复核员正在写结论）后再提交结论。提交时做条件 CAS——
  若这期间单子已被超时回收，则结论作废，不留下任何半截状态。
* 回收进程（reclaimer）：周期性扫描超过各自快照时限仍未写结论的「复核中」单，
  吐回待复核并记回收账。

两者是独立进程，靠 PostgreSQL 行锁串行化：同一次占位「写结论」与「回收」只有一个能赢。
"""

import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import django


def setup_django() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 认领 + 写结论


def claim_and_hold(hold_seconds: float) -> bool:
    """认领一条待复核单，占位 hold_seconds 后尝试写结论。返回是否认领过单。"""
    from desk.services import claim_next_pending, finalize_submission

    submission = claim_next_pending()
    if submission is None:
        return False

    print(
        f"claimed #{submission.id} {submission.tool_code} at "
        f"{submission.claimed_at:%H:%M:%S} limit={submission.claim_limit_seconds}s; "
        f"holding {hold_seconds:.1f}s before verdict",
        flush=True,
    )
    time.sleep(hold_seconds)

    claimed_at = submission.claimed_at
    ok = finalize_submission(submission.id, claimed_at)
    if ok:
        print(f"verdict written for #{submission.id}", flush=True)
    else:
        # 占位已被回收进程吐回待复核：本次结论作废，状态以「待复核」为准。
        print(
            f"verdict for #{submission.id} discarded: hold was reclaimed first",
            flush=True,
        )
    return True


def run_worker_loop() -> None:
    hold_seconds = _env_float("WORKER_HOLD_SECONDS", 5.0)
    poll_seconds = _env_float("WORKER_POLL_SECONDS", 0.5)
    print(
        f"cnc-offset worker started (hold={hold_seconds:.1f}s poll={poll_seconds:.2f}s)",
        flush=True,
    )
    while True:
        held = claim_and_hold(hold_seconds)
        if not held:
            time.sleep(poll_seconds)


# ---------------------------------------------------------------- 超时回收


def run_reclaim_loop() -> None:
    poll_seconds = _env_float("RECLAIM_POLL_SECONDS", 0.5)
    print(
        f"cnc-offset reclaimer started (poll={poll_seconds:.2f}s)",
        flush=True,
    )
    # 延迟导入，便于 manage.py 之外直接运行。
    from desk.services import reclaim_expired, reconcile

    while True:
        records = reclaim_expired()
        for rec in records:
            print(
                f"reclaimed #{rec.submission_id} {rec.tool_code} back to pending "
                f"(held {rec.held_seconds:.1f}s / limit {rec.limit_seconds}s)",
                flush=True,
            )
        check = reconcile()
        if not check["ok"]:
            print(f"RECONCILE MISMATCH: {check['mismatches']}", flush=True)
        time.sleep(poll_seconds)


def main() -> None:
    setup_django()
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "once":
            claim_and_hold(_env_float("WORKER_HOLD_SECONDS", 0.0))
        elif cmd == "reclaim-once":
            from desk.services import reclaim_expired, reconcile

            done = reclaim_expired()
            for rec in done:
                print(f"reclaimed #{rec.submission_id} {rec.tool_code}")
            print("reconcile:", reconcile())
        elif cmd == "reclaimer":
            run_reclaim_loop()
        else:
            print(f"unknown command: {cmd}", file=sys.stderr)
            sys.exit(2)
    else:
        run_worker_loop()


if __name__ == "__main__":
    main()
