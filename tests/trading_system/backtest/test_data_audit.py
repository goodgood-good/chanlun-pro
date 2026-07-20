from chanlun.decision_support.trading_system.backtest.data_audit import audit_dataset
from tests.trading_system.backtest.helpers import dataset


def test_current_sector_membership_cannot_certify_historical_backtest() -> None:
    evidence = audit_dataset(
        dataset(
            membership_as_of_each_session=False,
            point_in_time_adjustment=True,
        )
    )

    assert evidence.grade == "research_only"
    assert "historical_sector_membership_missing" in evidence.failures


def test_missing_listing_status_invalidates_dataset() -> None:
    evidence = audit_dataset(dataset(statuses=()))

    assert evidence.grade == "invalid"
    assert "market_data_missing" in evidence.failures
    assert "security_status_coverage_missing" in evidence.failures
