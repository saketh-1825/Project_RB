class GoBackendError(Exception):
    def __init__(
        self,
        status_code: int,
        message: str,
        original_exception: Exception | None = None
    ):
        self.status_code = status_code
        self.message = message
        self.original_exception = original_exception
        # Default error category (can be overridden by subclasses)
        self.error_category = "backend_unavailable"
        super().__init__(message)


class BackendTimeoutError(GoBackendError):
    def __init__(
        self,
        status_code: int,
        message: str,
        original_exception: Exception | None = None
    ):
        super().__init__(status_code, message, original_exception)
        self.error_category = "timeout"


class BackendUnavailableError(GoBackendError):
    def __init__(
        self,
        status_code: int,
        message: str,
        original_exception: Exception | None = None
    ):
        super().__init__(status_code, message, original_exception)
        if status_code == 500:
            self.error_category = "server_error"
        else:
            self.error_category = "backend_unavailable"


class BackendNotFoundError(GoBackendError):
    def __init__(
        self,
        status_code: int,
        message: str,
        original_exception: Exception | None = None
    ):
        super().__init__(status_code, message, original_exception)
        self.error_category = "not_found"
