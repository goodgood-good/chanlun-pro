from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

from tests.web.test_tv_drawing_state_contract import app as app
from tests.web.test_tv_drawing_state_contract import _install_fake_store, URL, SCHEMA
from cl_app.services.drawing_merge import merge


def empty():
    return {"schema": SCHEMA, "sources": {}, "groups": {}}


def drawing(period, price=10):
    return {
        "type": "LineToolTrendLine",
        "_cl_origin_resolution": period,
        "state": {"interval": period, "price": price},
    }


def test_native_zorder_renumber_after_remote_delete_allows_an_unrelated_addition():
    base = empty()
    base["sources"] = {
        name: {**drawing("5"), "state": {"price": 10, "zorder": z}}
        for name, z in [("old", -10000.001), ("other", -10000)]
    }
    local = deepcopy(base)
    local["sources"]["old"]["state"]["zorder"] = -15000
    local["sources"]["other"]["state"]["zorder"] = -12500
    local["sources"]["new"] = {**drawing("5"), "state": {"price": 12, "zorder": -10000}}
    current = deepcopy(base)
    del current["sources"]["old"]
    result, conflicts = merge(base, local, current)
    assert not conflicts and set(result["sources"]) == {"other", "new"}
    assert result["sources"]["other"]["state"]["zorder"] == -12500


def test_real_geometry_edit_or_relative_reorder_remains_a_conflict_after_remote_delete():
    base = empty()
    base["sources"] = {
        name: {**drawing("5"), "state": {"price": 10, "zorder": z}}
        for name, z in [("old", -2), ("other", -1)]
    }
    current = deepcopy(base)
    del current["sources"]["old"]
    for change in ({"zorder": 0}, {"price": 11}):
        local = deepcopy(base)
        local["sources"]["old"]["state"].update(change)
        assert merge(base, local, current)[1] == ["old"]


def test_two_windows_can_add_different_period_drawings_without_overwriting(
    app, monkeypatch
):
    _install_fake_store(monkeypatch)
    url = URL.replace("resolution=all", "resolution=hierarchy")
    base = empty()
    base["sources"]["original"] = drawing("30")
    with app.test_client() as client:
        assert (
            client.post(url, json={"state": base, "base_state": empty()}).status_code
            == 200
        )
        baseline = client.get(url).get_json()["data"]
    first = deepcopy(baseline)
    first["sources"]["new5"] = drawing("5")
    second = deepcopy(baseline)
    second["sources"]["new1"] = drawing("1")

    def save(state):
        with app.test_client() as client:
            return client.post(
                url, json={"state": state, "base_state": baseline}
            ).get_json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(save, [first, second]))
    assert all(r["status"] == "ok" for r in responses)
    with app.test_client() as client:
        assert set(client.get(url).get_json()["data"]["sources"]) == {
            "original",
            "new5",
            "new1",
        }
        # The second window has not displayed new5 yet. Its next save must not
        # interpret that unseen server addition as an intentional deletion.
        changed = deepcopy(second)
        changed["sources"]["new1"] = drawing("1", 11)
        response = client.post(url, json={"state": changed, "base_state": second})
        assert response.status_code == 200
        assert set(response.get_json()["data"]["sources"]) == {
            "original",
            "new5",
            "new1",
        }


def test_same_shape_edits_and_stale_deletions_are_conflicts(app, monkeypatch):
    _install_fake_store(monkeypatch)
    with app.test_client() as client:
        base = empty()
        base["sources"]["one"] = drawing("5")
        client.post(URL, json={"state": base, "base_state": empty()})
        first = deepcopy(base)
        first["sources"]["one"] = drawing("5", 11)
        assert (
            client.post(URL, json={"state": first, "base_state": base}).status_code
            == 200
        )
        second = deepcopy(base)
        second["sources"]["one"] = drawing("5", 12)
        for stale in (second, empty()):
            response = client.post(URL, json={"state": stale, "base_state": base})
            assert response.status_code == 409 and response.get_json()["conflicts"] == [
                "one"
            ]
        assert client.get(URL).get_json()["data"] == first


def test_unchanged_old_shapes_do_not_resurrect_a_remote_deletion(app, monkeypatch):
    _install_fake_store(monkeypatch)
    with app.test_client() as client:
        base = empty()
        base["sources"]["removed"] = drawing("30")
        client.post(URL, json={"state": base, "base_state": empty()})
        client.post(URL, json={"state": empty(), "base_state": base})
        new = deepcopy(base)
        new["sources"]["added"] = drawing("1")
        response = client.post(URL, json={"state": new, "base_state": base})
        assert set(response.get_json()["data"]["sources"]) == {"added"}
        # Old bundles without a baseline cannot restore or overwrite anything.
        response = client.post(URL, json={"state": base})
        assert response.status_code == 409
        assert set(client.get(URL).get_json()["data"]["sources"]) == {"added"}


def test_old_bundle_cannot_recreate_a_drawing_after_all_were_deleted(app, monkeypatch):
    _install_fake_store(monkeypatch)
    with app.test_client() as client:
        old = empty()
        old["sources"]["old"] = drawing("5")
        response = client.post(URL, json={"state": old})
        assert response.status_code == 409
        assert client.get(URL).get_json()["data"] == empty()
