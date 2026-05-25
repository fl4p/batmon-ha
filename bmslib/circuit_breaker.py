import time


class CircuitBreaker:
    """Decides when to (re)attempt sending after server failures.

    backoff_interval == 0 disables it: always attempt, never buffer
    (preserves the historical InfluxDBSink behavior).

    When enabled, a failure blocks further attempts for backoff_interval
    seconds. Batches are worth buffering only after at least one successful
    write (keep_batch_on_failure) -- before that the server may be permanently
    unreachable for this user, so callers should drop instead of accumulate.
    """

    def __init__(self, backoff_interval: float = 0):
        self.backoff_interval = backoff_interval
        self.ever_succeeded = False
        self.backoff_until = 0.0

    @property
    def enabled(self) -> bool:
        return self.backoff_interval > 0

    def should_attempt(self, now: float = None) -> bool:
        if not self.enabled:
            return True
        if now is None:
            now = time.time()
        return now >= self.backoff_until

    def on_success(self, now: float = None) -> None:
        self.ever_succeeded = True
        self.backoff_until = 0.0

    def on_failure(self, now: float = None) -> None:
        if not self.enabled:
            return
        if now is None:
            now = time.time()
        self.backoff_until = now + self.backoff_interval

    @property
    def keep_batch_on_failure(self) -> bool:
        return self.enabled and self.ever_succeeded
