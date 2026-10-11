"""Apply the saved network route to each new public market-data request."""
import requests

from chanlun.utils import config_get_proxy


class ConfiguredProxySession(requests.Session):
    def __init__(self, proxy_reader=None):
        super().__init__()
        self.trust_env = False
        self._proxy_reader = proxy_reader or config_get_proxy

    def request(self, method, url, **kwargs):
        proxy = self._proxy_reader()
        address = f"http://{proxy['host']}:{proxy['port']}" if proxy.get("host") else None
        # A request-local mapping lets already-running requests finish on their
        # captured route. Direct access also overrides any stale client proxy.
        kwargs["proxies"] = {"http": address, "https": address} if address else {}
        return super().request(method, url, **kwargs)
