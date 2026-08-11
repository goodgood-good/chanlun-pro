import copy
import pickle

import pytest

from chanlun.core.cl import CL


def test_market_is_mandatory_and_normalized() -> None:
    with pytest.raises(ValueError, match="CL market is required"):
        CL("SH.600519", "30m", {})

    assert CL("SH.600519", "30m", {}, market=" A ").market == "a"
    assert CL("QQQ.US", "30m", {}, market="US").market == "us"


def test_current_pickle_schema_round_trips() -> None:
    current = CL("QQQ.US", "30m", {}, market="us")
    restored = pickle.loads(pickle.dumps(current))
    copied = copy.deepcopy(current)

    assert restored.market == "us"
    assert copied.market == "us"


def test_pickle_state_without_current_evidence_lock_is_rejected() -> None:
    current = CL("SH.600519", "30m", {}, market="a")
    incomplete = dict(current.__dict__)
    incomplete.pop("_strict_evidence_lock")
    restored = object.__new__(CL)

    with pytest.raises(ValueError, match="strict CL pickle schema is invalid"):
        restored.__setstate__(incomplete)
