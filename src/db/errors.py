"""Admission failures the job queue raises at enqueue — caught by every producer."""


class InsufficientCompute(Exception):
    def __init__(self, game_id: str, remaining: int, needed: int):
        super().__init__(f"game {game_id} has ${remaining / 1e6:.4f} of compute left, "
                         f"needs ${needed / 1e6:.4f}")
        self.game_id = game_id
        self.remaining = remaining
        self.needed = needed


class GamePaused(Exception):
    pass


class BuildEnded(Exception):
    pass
