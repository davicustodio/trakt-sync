from __future__ import annotations


class AmbiguousTitleError(Exception):
    def __init__(self, options: list[str]) -> None:
        super().__init__("Ambiguous TMDb match")
        self.options = options
