"""Input-contract failures stay distinct from structural calculation errors."""


class MarketDataContractError(ValueError):
    def __init__(self, reason, message):
        self.reason = reason
        super().__init__(f"{reason}: {message}")
