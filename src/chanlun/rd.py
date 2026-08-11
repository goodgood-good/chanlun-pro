"""Redis client used by the live IB market-data adapter."""

import redis

from chanlun import config


_client = None


def Robj():
    """Return the process-local text Redis client singleton."""
    global _client
    if _client is None:
        _client = redis.Redis(
            host=config.REDIS_HOST,
            port=config.REDIS_PORT,
            decode_responses=True,
        )
    return _client
