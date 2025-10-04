import time
from collections import defaultdict

class RateLimiter:
    def __init__(self):
        self.windows: dict[str, tuple[int, int]] = defaultdict(lambda: (0, 0))

    def allow(self, ip: str, route: str, per_minute: int) -> bool:
        """Checks if a request is allowed under a fixed-window rate limit."""
        now = int(time.time())
        current_window = now // 60
        key = f"{ip}:{route}" # Simplified key for per-route limit

        window_start, count = self.windows.get(key, (0, 0))

        if window_start != current_window:
            # New window, reset count
            self.windows[key] = (current_window, 1)
            return True

        if count >= per_minute:
            # Exceeded limit
            return False

        # Increment count in current window
        self.windows[key] = (current_window, count + 1)
        return True
