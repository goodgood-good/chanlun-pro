import gzip
import json
import pytest

from chanlun.screening.calculation_revision import computation_digest
from tests.test_screening_selection_reuse import selection as selection, semantic


@pytest.mark.parametrize('execution_file', ['compute_capacity.py', 'worker_pool.py', 'worker_transport.py'])
def test_execution_change_preserves_verified_structure_but_unknown_code_invalidates(tmp_path, execution_file):
    root=tmp_path/'src/chanlun'
    for name in ('core/cl.py','screening/compute_capacity.py','screening/rules.py'):
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('baseline')
    for name in ('pyproject.toml','poetry.lock'):(tmp_path/name).write_text('dependencies')
    before=computation_digest(root,tmp_path)
    (root/'screening'/execution_file).write_text('new execution metadata')
    assert computation_digest(root,tmp_path)==before
    (root/'screening/new_dependency.py').write_text('unknown dependency')
    assert computation_digest(root,tmp_path)!=before
    current=computation_digest(root,tmp_path)
    (root/'core/cl.py').write_text('new algorithm')
    assert computation_digest(root,tmp_path)!=current


def test_runtime_only_change_reuses_structure_and_republishes_current_metadata(selection,monkeypatch):
    from chanlun.screening import calculation_revision
    monkeypatch.setattr(calculation_revision,'calculation_contract',lambda revision:'same-algorithm')
    scan,_,_,_,counts,runs=selection
    first=scan(revision='runtime-v1')
    second=scan(revision='runtime-v2')
    assert second['calculation_reused'] and second['selection_reused'] and counts['audits']==1
    assert semantic(first)==semantic(second)
    path=runs[0].parent/'cache/SH.600000_1m.json.gz'
    payload=json.loads(json.loads(gzip.decompress(path.read_bytes()))['payload'])
    assert payload['calculation_revision']=='runtime-v2'
    assert payload['calculation_contract_revision']=='same-algorithm'
    monkeypatch.setattr(calculation_revision,'calculation_contract',lambda revision:'changed-algorithm')
    assert not scan(revision='runtime-v3')['calculation_reused']


def test_reused_selection_keeps_this_reads_source_verification_clock(selection):
    scan,frame,*_=selection
    frame.attrs['_screening_history_cache']={'full_checked_at':100,'checked_at':100,'through':100,'mode':'full'}
    first=scan()
    frame.attrs['_screening_history_cache']={**frame.attrs['_screening_history_cache'],'checked_at':110,'mode':'shared'}
    second=scan()
    assert second['selection_reused']
    assert first['history_refresh']['checked_at']==100 and second['history_refresh']['checked_at']==110


@pytest.mark.parametrize('module',['quote_capacity.py','longbridge_quote.py','longbridge_history_limit.py','usmart_rate_limit.py'])
def test_transport_only_changes_preserve_contract_but_time_and_price_normalization_invalidates(tmp_path,module):
    root=tmp_path/'src/chanlun';exchange=root/'exchange';exchange.mkdir(parents=True)
    (exchange/'exchange_cq.py').write_text('price and time normalization')
    for name in ('pyproject.toml','poetry.lock'):(tmp_path/name).write_text('dependencies')
    before=computation_digest(root,tmp_path)
    (exchange/module).write_text('bounded transport scheduling')
    assert computation_digest(root,tmp_path)==before
    (exchange/'exchange_cq.py').write_text('changed price or timestamp normalization')
    assert computation_digest(root,tmp_path)!=before
