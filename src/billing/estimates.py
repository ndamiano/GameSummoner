"""What a job is expected to cost, and what one actually did."""

import math

MICRO = 1_000_000

# p90 of jobs.exec_seconds on prod, read 2026-09-11.
QUEUE_SECONDS_ESTIMATES = {
    "llm":   31,
    "image": 33,
    "decide": 20,
}

# The most expensive card each queue's pods are created on, priced at RunPod's rate.
QUEUE_USD_PER_HOUR = {
    "llm":   2.21,   # RTX PRO 6000 Blackwell
    "image": 1.01,   # RTX 5090
    "decide": 2.21,  # RTX PRO 6000 Blackwell
}


def calculate_job_cost(exec_seconds: float, usd_per_hour: float) -> int:
    rate_micros = math.ceil(usd_per_hour * MICRO / 3600)
    return math.ceil(exec_seconds * rate_micros)


QUEUE_MICRO_ESTIMATES = {
    queue: calculate_job_cost(seconds, QUEUE_USD_PER_HOUR[queue])
    for queue, seconds in QUEUE_SECONDS_ESTIMATES.items()
}
