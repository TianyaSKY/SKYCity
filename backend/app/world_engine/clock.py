"""WorldClock: converts real time into game minutes.

Model: world_time is an integer count of game minutes since the world epoch
(480 = 08:00 of day 1; day = world_time // 1440 + 1). The engine ticks every
0.1 real seconds; speed (1/2/5/10) is game minutes per real second. Fractional
minutes accumulate and each crossed integer minute is emitted once.
"""

from __future__ import annotations

from app.database.models.worlds import DEFAULT_SPEED, DEFAULT_WORLD_TIME

MINUTES_PER_DAY = 1440

VALID_SPEEDS = (1, 2, 5, 10)


class WorldClock:
    """In-memory clock for one world runtime; the engine persists it to DB."""

    __slots__ = ("world_time", "speed", "paused", "_accumulator")

    def __init__(
        self,
        world_time: int = DEFAULT_WORLD_TIME,
        speed: int = DEFAULT_SPEED,
        paused: bool = False,
    ) -> None:
        if speed not in VALID_SPEEDS:
            raise ValueError(f"invalid speed {speed!r}, expected one of {VALID_SPEEDS}")
        self.world_time = world_time
        self.speed = speed
        self.paused = paused
        self._accumulator = 0.0

    # ------------------------------------------------------------------ #
    # Tick
    # ------------------------------------------------------------------ #

    def tick(self, dt_seconds: float) -> list[int]:
        """Advance the clock by ``dt_seconds`` real seconds.

        Returns the world_time values of every integer minute crossed (may be
        empty when paused, speed-less, or no boundary was crossed).
        """
        if self.paused:
            return []
        self._accumulator += dt_seconds * self.speed
        crossed: list[int] = []
        while self._accumulator >= 1.0:
            self._accumulator -= 1.0
            self.world_time += 1
            crossed.append(self.world_time)
        return crossed

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @property
    def day(self) -> int:
        return self.world_time // MINUTES_PER_DAY + 1

    def minute_of_day(self) -> int:
        return self.world_time % MINUTES_PER_DAY

    def format_time(self) -> str:
        minutes = self.minute_of_day()
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    def is_day_boundary(self) -> bool:
        return self.world_time % MINUTES_PER_DAY == 0

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"WorldClock(world_time={self.world_time}, speed={self.speed}, "
            f"paused={self.paused}, time={self.format_time()})"
        )
