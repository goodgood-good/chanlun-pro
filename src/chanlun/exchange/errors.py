"""Public provider failures without importing optional exchange SDKs."""


class USmartAPIError(RuntimeError):
    """uSMART HTTP or business failure, excluding credentials and request bodies."""

    def __init__(self, message: str, *, code=None, status_code: int | None = None, retry_after=None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retry_after = retry_after


class LongbridgeConnectionLimitError(RuntimeError):
    """The provider rejected a quote connection; data is unavailable, not empty."""

    code = "longbridge_connection_limit"
    retry_after = 30
    public_message = "行情连接已达上限，请稍后重试"

    def __init__(self, retry_after=30):
        self.retry_after = max(1, int(retry_after))
        super().__init__(self.public_message)

    def __reduce__(self):
        return type(self), (self.retry_after,)
