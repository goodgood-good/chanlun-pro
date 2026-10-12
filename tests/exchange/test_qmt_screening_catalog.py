"""Screening discovery uses current membership and bounded metadata reuse."""
from copy import deepcopy
import json
from unittest.mock import Mock

import pytest

from chanlun.exchange import qmt_screening_catalog as catalog
from chanlun.exchange.exchange_qmt import ExchangeQMT

NOW = 1791784800
ROWS = [{'code':code,'name':code,'type':'stock_cn'} for code in ['SH.600000','SZ.000001','BJ.920001']]
NATIVE = ['600000.SH','000001.SZ','920001.BJ']


def provider(listed=None):
    value=Mock()
    value.get_stock_list_in_sector.return_value=NATIVE if listed is None else listed
    value.get_instrument_type.return_value={'stock':True}
    value.get_instrument_detail.side_effect=lambda code,complete:{'InstrumentName':'fresh-'+code}
    value.get_full_tick.side_effect=AssertionError('screening must not enumerate quote instruments')
    return value


def cache(root,rows=None,**changes):
    path=root/'cache/qmt_screening_catalog.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    rows=deepcopy(ROWS if rows is None else rows)
    payload={'schema':catalog.SCHEMA,'stocks':rows,'count':len(rows),'updated_at':NOW-60,**changes}
    path.write_text(json.dumps(payload),encoding='utf-8')


def test_current_catalog_reuses_metadata_without_quote_or_per_symbol_native_calls(tmp_path):
    cache(tmp_path)
    native=provider()
    rows,metrics=catalog.read_stocks(native,root=tmp_path,now=NOW)
    assert rows==ROWS and metrics['metadata_reused']==3 and metrics['native_type_reads']==0
    native.get_full_tick.assert_not_called()
    native.get_instrument_type.assert_not_called()
    native.get_stock_list_in_sector.assert_called_once_with('沪深京A股')


def test_new_member_is_inspected_and_removed_member_never_returns_from_cache(tmp_path):
    cache(tmp_path)
    native=provider(['600001.SH','000001.SZ','920001.BJ'])
    rows,metrics=catalog.read_stocks(native,root=tmp_path,now=NOW)
    assert {row['code'] for row in rows}=={'SH.600001','SZ.000001','BJ.920001'}
    native.get_instrument_type.assert_called_once_with('600001.SH')
    native.get_instrument_detail.assert_called_once_with('600001.SH',False)
    assert rows[0]['name']=='fresh-600001.SH' and metrics['metadata_reused']==2


@pytest.mark.parametrize('damage',[
    {'updated_at':NOW-3601},{'updated_at':NOW+1},{'updated_at':True},
    {'stocks':[dict(ROWS[0],name=''),*ROWS[1:]]},
    {'stocks':[dict(ROWS[0],type='unknown'),*ROWS[1:]]},
    {'count':2},{'schema':'chanlun-stock-list-cache'},
    {'stocks':[ROWS[0],ROWS[0],ROWS[2]]},
])
def test_stale_or_malformed_cache_requires_fresh_metadata(tmp_path,damage):
    cache(tmp_path,**damage)
    native=provider()
    rows,metrics=catalog.read_stocks(native,root=tmp_path,now=NOW)
    assert metrics['metadata_reused']==0 and native.get_instrument_type.call_count==3
    assert all(row['name'].startswith('fresh-') for row in rows)


def test_metadata_clock_is_not_extended_by_repeated_reuse(tmp_path):
    cache(tmp_path)
    catalog.read_stocks(provider(),root=tmp_path,now=NOW)
    own=tmp_path/'cache/qmt_screening_catalog.json'
    assert json.loads(own.read_text())['updated_at']==NOW-60
    _,metrics=catalog.read_stocks(provider(),root=tmp_path,now=NOW+3500)
    assert metrics['metadata_reused']==3
    _,metrics=catalog.read_stocks(provider(),root=tmp_path,now=NOW+3541)
    assert metrics['metadata_reused']==0


def test_bonds_index_and_unknown_nonstock_flags_in_sector_are_not_selected(tmp_path):
    native=provider([*NATIVE,'821023.BJ','899050.BJ','810011.BJ'])
    native.get_instrument_type.side_effect=lambda code:{'bond':True} if code=='821023.BJ' else (
        {'index':True} if code=='899050.BJ' else {} if code=='810011.BJ' else {'stock':True})
    rows,_=catalog.read_stocks(native,root=tmp_path,now=NOW)
    assert {row['code'] for row in rows}=={row['code'] for row in ROWS}
    assert native.get_instrument_detail.call_count==3


@pytest.mark.parametrize('listed',[[],['600000.SH'],[*NATIVE,'malformed']])
def test_partial_or_invalid_live_listing_is_not_replaced_with_cached_membership(tmp_path,listed):
    cache(tmp_path)
    with pytest.raises(ValueError,match='目录不完整'):
        catalog.read_stocks(provider(listed),root=tmp_path,now=NOW)


def test_missing_new_stock_details_do_not_publish_a_shortened_successful_catalog(tmp_path):
    native=provider()
    native.get_instrument_detail.return_value=None
    native.get_instrument_detail.side_effect=None
    with pytest.raises(ValueError,match='证券名称'):
        catalog.read_stocks(native,root=tmp_path,now=NOW)
    assert not (tmp_path/'cache/qmt_screening_catalog.json').exists()


@pytest.mark.parametrize('authorization',[False,None,1,'true'])
def test_stock_only_catalog_still_requires_exact_authorization_before_io(monkeypatch,authorization):
    read=Mock(side_effect=AssertionError('unauthorized native call'))
    monkeypatch.setattr(catalog,'read_stocks',read)
    with pytest.raises(PermissionError):
        ExchangeQMT().screening_stocks(full_market_authorized=authorization)
    read.assert_not_called()
