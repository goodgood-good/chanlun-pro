"""Public provider failures without importing optional exchange SDKs."""


class USmartAPIError(RuntimeError):
    """uSMART HTTP or business failure, excluding credentials and request bodies."""

    def __init__(self, message: str, *, code=None, status_code: int | None = None, retry_after=None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retry_after = retry_after


class USmartHistoryPending(USmartAPIError):
    public_message = "该标的历史行情正在更新，请稍后重试。"

    def __init__(self):
        super().__init__(self.public_message,code="usmart_history_pending",retry_after=3)

    def __reduce__(self):
        return type(self), ()


class SourcePreparationPending(USmartHistoryPending):
    public_message = '行情数据正在排队或准备完整窗口，请稍后复核。'

    def __init__(self, preparation=None, code='source_preparation_pending'):
        super().__init__()
        self.preparation=dict(preparation or {})
        self.code=code
        self.retry_after=max(1,min(300,int(self.preparation.get('retry_after',3))))

    def __reduce__(self):
        return type(self),(self.preparation,self.code)


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
