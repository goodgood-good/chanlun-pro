"""List performance must preserve evidence identity and partial-result semantics."""
import gzip

import pytest
from flask import Flask

from cl_app.blueprints.screening import screening_bp
from cl_app.services import screening_workbench as workbench


@pytest.mark.parametrize('loading', [False, True])
@pytest.mark.parametrize('accept,compressed', [('gzip', True), ('gzip;q=0, identity', False),
    ('gzip;q=0.5, identity;q=1', False), ('', False)])
def test_result_transport_preserves_verified_rows_and_loading_status(monkeypatch, loading, accept, compressed):
    payload = {'status': {'results_loading': loading}, 'candidates': [
        {'id': str(i), 'point': {'status': 'approaching'}, 'explanation': '周期联动证据' * 20} for i in range(30)]}
    monkeypatch.setattr(workbench, 'dashboard', lambda **_: payload)
    app = Flask(__name__)
    app.config['LOGIN_DISABLED'] = True
    app.register_blueprint(screening_bp)
    response = app.test_client().get('/screening/workbench?compact=1', headers={'Accept-Encoding': accept})
    assert response.status_code == (202 if loading else 200)
    assert response.headers['Cache-Control'] == 'private, no-store'
    assert 'Accept-Encoding' in response.vary
    assert bool(response.headers.get('Content-Encoding')) is compressed
    raw = gzip.decompress(response.data) if compressed else response.data
    assert app.json.loads(raw) == payload
    if compressed:
        assert len(response.data) < len(raw) / 4
    if loading:
        assert response.headers['Retry-After'] == '5'


def test_compact_projection_excludes_heavy_proof_before_copy_but_keeps_linkage_identity():
    point = {'point_id': 'point', 'point_type': '2buy', 'status': 'approaching',
        'anchor_at': 300, 'anchor_price': 10, 'terminal_start_at': 100,
        'price_basis_revision': 'raw', 'proof': {'history': list(range(5000))}}
    item = {'code': 'SH.600000', 'frequency': '5m', 'point': point,
        'input_fingerprint': 'input-v1', 'source_closed_at': 360}
    result = workbench._dashboard_projection({'run_id': 'run', 'observations': [item]}, True)
    row = result['_workbench_rows'][0]
    assert 'proof' not in row['point']
    assert row['input_fingerprint'] == 'input-v1'
    assert row['point']['price_basis_revision'] == 'raw'
    assert row['point']['terminal_start_at'] == 100
    assert row['point']['anchor_at'] == 300
    assert row['point']['anchor_price'] == 10
    assert row['detail_loaded'] is False
    assert point['proof']['history'][-1] == 4999
    changed = {**item, 'point': {**point, 'proof': {'history': [1]}}}
    later = workbench._dashboard_projection({'run_id': 'run', 'observations': [changed]}, True)
    assert later['_workbench_rows'][0]['candidate_evidence_version'] != row['candidate_evidence_version']


def test_compact_linkage_keeps_explanations_without_copying_peer_proofs():
    original = {'category': 'mixed', 'quality': 'complete', 'notes': ['1m 有反向信号'],
        'periods': [{'frequency': '1m', 'role': 'timing', 'label': '一卖预警',
            'source_closed_at': 300, 'anchor_aligned': False, 'signal': {'point_id': 'sell'},
            'center': {'zd': 9, 'zg': 10}}]}
    compact = workbench._compact_context(original)
    assert compact['notes'] == original['notes']
    assert compact['category'] == 'mixed'
    assert compact['periods'][0]['label'] == '一卖预警'
    assert compact['periods'][0]['source_closed_at'] == 300
    assert 'signal' not in compact['periods'][0] and 'center' not in compact['periods'][0]
    assert original['periods'][0]['signal']['point_id'] == 'sell'
