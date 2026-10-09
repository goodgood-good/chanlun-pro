import gzip
import json

from cl_app.services.screening_rollover import RollingDisplay, FILENAME


A, B, C = 'a'*32, 'b'*32, 'c'*32


def row(code, frequency='5m', identity=None):
    return {'id': identity or code+frequency, 'market':'a','code':code,'frequency':frequency,
            'point':{'point_id':identity or code, 'point_type':'1buy','available_at':100},
            'source_closed_at':200,'origin':'screening','stage':'confirmed'}


def run(root, source, status='running', **extra):
    (root/'latest.json').write_text(json.dumps({'run_id':source}),encoding='utf-8')
    return {'run_id':source,'status':status,'completed':0,**extra}


def merge(view, result, rows=(), live_filter=None):
    return view.merge(result, list(rows), project=lambda payload, source:payload['fixture_rows'],
                      live_filter=live_filter or (lambda rows,*_: (rows,0,0,0)))


def test_new_scan_keeps_previous_rows_after_page_and_process_reload(tmp_path):
    view=RollingDisplay(tmp_path)
    merge(view,run(tmp_path,A,'completed'),[row('SH.600001'),row('SH.600002')])
    result=run(tmp_path,B,results_loading=True,results_available=False)
    rows,count=merge(view,result)
    assert count==2 and all(r['refresh_state']=='pending' and r['result_source']==A for r in rows)
    restored,restored_count=merge(RollingDisplay(tmp_path),result)
    assert restored==rows and restored_count==2
    assert all(r['evidence_chart_available'] is False for r in rows)


def test_successful_empty_period_removes_old_point_while_new_matches_arrive(tmp_path):
    view=RollingDisplay(tmp_path)
    merge(view,run(tmp_path,A,'completed'),[row('SH.600001'),row('SH.600002')])
    result=run(tmp_path,B,completed=2,_workbench_verified=[('a','SH.600001','5m'),('a','SH.600003','5m')])
    rows,count=merge(view,result,[row('SH.600003')])
    assert [r['code'] for r in rows]==['SH.600003','SH.600002'] and count==1
    # The removed point cannot reappear on a subsequent partial response.
    assert [r['code'] for r in merge(view,result,[row('SH.600003')])[0]]==['SH.600003','SH.600002']


def test_replacement_is_per_market_symbol_period_and_scope(tmp_path):
    view=RollingDisplay(tmp_path)
    merge(view,run(tmp_path,A,'completed'),[row('SH.600001','1m'),row('SH.600001','5m'),row('SH.600002')])
    result=run(tmp_path,B,settings={'scope':'codes','codes':['SH.600001'],'frequencies':['1m','5m']},
               _workbench_verified=[('a','SH.600001','1m')])
    rows,count=merge(view,result)
    assert [(r['code'],r['frequency']) for r in rows]==[('SH.600001','5m')] and count==1


def test_failed_checks_stay_labelled_until_final_result_and_completed_empty_is_authoritative(tmp_path):
    view=RollingDisplay(tmp_path)
    merge(view,run(tmp_path,A,'completed'),[row('SH.600001')])
    result=run(tmp_path,B,completed=1,_workbench_processed=[('a','SH.600001')])
    rows,count=merge(view,result)
    assert count==1 and rows[0]['refresh_state']=='limited'
    rows,count=merge(view,{**result,'status':'completed','results_loading':False})
    assert rows==[] and count==0
    assert merge(view,run(tmp_path,C))[0]==[]


def test_cancel_then_restart_carries_the_latest_partial_display(tmp_path):
    view=RollingDisplay(tmp_path)
    merge(view,run(tmp_path,A,'completed'),[row('SH.600001'),row('SH.600002')])
    partial=run(tmp_path,B,'cancelled',completed=1,_workbench_verified=[('a','SH.600001','5m')])
    merge(view,partial,[row('SH.600001',identity='replacement')])
    rows,count=merge(view,run(tmp_path,C))
    assert count==2
    assert {r['id']:r['result_source'] for r in rows}=={'replacement':B,'SH.6000025m':A}


def test_saved_verified_result_bootstraps_a_page_not_opened_during_prior_scan(tmp_path):
    directory=tmp_path/A;directory.mkdir()
    (directory/'status.json').write_text(json.dumps({'run_id':A,'status':'completed','started_at':100}))
    payload={'schema':'screening-verified-results-v1','payload':{'run_id':A,'fixture_rows':[row('SH.600001')]}}
    (directory/'verified_results.json.gz').write_bytes(gzip.compress(json.dumps(payload).encode()))
    rows,count=merge(RollingDisplay(tmp_path),run(tmp_path,B,started_at=200))
    assert count==1 and rows[0]['result_source']==A


def test_new_live_exit_proof_removes_previous_result(tmp_path):
    view=RollingDisplay(tmp_path)
    merge(view,run(tmp_path,A,'completed'),[row('SH.600001')])
    rows,count=merge(view,run(tmp_path,B),live_filter=lambda rows,*_: ([],1,1,0))
    assert rows==[] and count==0
    assert merge(view,run(tmp_path,B))[0]==[]


def test_old_requests_cannot_overwrite_newer_saved_progress(tmp_path):
    view=RollingDisplay(tmp_path)
    merge(view,run(tmp_path,A,'completed'),[row('SH.600001')])
    result=run(tmp_path,B,results_through=2,_workbench_verified=[('a','SH.600001','5m')])
    merge(view,result,[row('SH.600002')])
    merge(view,{**result,'results_through':1},[row('SH.600003')])
    saved=json.loads(gzip.decompress((tmp_path/FILENAME).read_bytes()))
    assert saved['through']==2 and [r['code'] for r in saved['rows']]==['SH.600002']


def test_reader_restart_keeps_already_published_current_rows_labelled_while_loading(tmp_path):
    view=RollingDisplay(tmp_path)
    result=run(tmp_path,A,results_through=64,results_available=True)
    merge(view,result,[row('SH.600001'),row('SH.600002')])
    rows,count=merge(RollingDisplay(tmp_path),{**result,'results_through':0,'results_loading':True,'results_available':False})
    assert count==2 and all(row['refresh_state']=='pending' for row in rows)
    recovered,count=merge(RollingDisplay(tmp_path),{**result,'results_loading':False},[row('SH.600002')])
    assert [r['code'] for r in recovered]==['SH.600002'] and count==0
