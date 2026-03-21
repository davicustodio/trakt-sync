from __future__ import annotations


class AmbiguousTitleError(Exception):
    def __init__(self, options: list[str]) -> None:
        super().__init__("Ambiguous TMDb match")
        self.options = options


class VisionIdentificationError(Exception):
    def __init__(self, reason: str, attempts: list[str] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.attempts = attempts or []
