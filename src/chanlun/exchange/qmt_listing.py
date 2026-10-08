"""Remove zero-volume IPO placeholders using independently supplied stock metadata."""
from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime
from time import monotonic


class QmtListingBoundary:
    """Bound metadata reads; the caller holds the QMT native API lock."""
    def __init__(self):
        self._cache = OrderedDict()

    def listing_date(self, code, provider):
        now = monotonic()
        cached = self._cache.get(code)
        if cached is not None and now < cached[0]:
            self._cache.move_to_end(code)
            return cached[1]
        listed = None
        try:
            kinds = provider.get_instrument_type(code)
            if isinstance(kinds, Mapping) and kinds.get("stock") is True and not kinds.get("index") and not kinds.get("etf"):
                details = provider.get_instrument_detail(code, False)
                symbol, market = code.split(".")
                if (isinstance(details, Mapping) and str(details.get("InstrumentID", "")).zfill(6) == symbol
                        and str(details.get("ExchangeID", "")).upper() == market):
                    value = str(details.get("OpenDate", ""))
                    # These values describe subscription/instrument classes,
                    # not dates: see the vendor stock-data dictionary.
                    if len(value) == 8 and value.isdigit() and value not in {f"1970010{i}" for i in range(1, 7)}:
                        listed = datetime.strptime(value, "%Y%m%d").date()
        except Exception:
            # An absent date cannot authorize trimming any source history.
            listed = None
        self._cache[code] = (now + (3600 if listed is not None else 60), listed)
        self._cache.move_to_end(code)
        while len(self._cache) > 2048:
            self._cache.popitem(last=False)
        return listed


def normalize_stock_listing_rows(frame, listed, *, code):
    if listed is None or frame.empty:
        return frame
    before = frame.date.dt.date < listed
    if not before.any():
        return frame
    metadata = {"qmt_listing_date": listed.isoformat(), "qmt_listing_code": code,
                "qmt_listing_source": "xtdata.get_instrument_detail.OpenDate"}
    if (frame.loc[before, "volume"] != 0).any():
        # Conflicting actual trades must be investigated, never discarded to
        # make a broken history look complete.
        result = frame.copy()
        result.attrs.update(metadata, qmt_listing_history_conflict=True)
        return result
    result = frame.loc[~before].copy().reset_index(drop=True)
    result.attrs.update(metadata, qmt_prelisting_rows_removed=int(before.sum()))
    return result
