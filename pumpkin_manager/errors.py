"""Exception base class for failures whose message is meant to be shown to the user."""


class UserError(Exception):
    """Expected, explainable failure (bad input, busy server, unreachable GitHub, ...).

    Anything *not* derived from this class is treated as a bug: it is logged with a traceback and the
    user only sees a generic message. ``status`` is the HTTP status used when it reaches the API.
    """

    status = 400

    def __init__(self, message: str = "", status: int | None = None):
        super().__init__(message)
        if status is not None:
            self.status = status
