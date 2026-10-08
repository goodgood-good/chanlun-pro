"""Large job inputs cannot defeat worker startup deadlines or cancellation."""
import multiprocessing
import os
import pickle
import time
from pathlib import Path

import pytest

from chanlun.screening.worker_pool import ReusableWorkerPool
from chanlun.screening.runner import bounded_scan
from chanlun.screening.worker_transport import MAX_CONTROL_BYTES, JobInputReader, bounded_message, store_job, store_reply, read_reply, discard_reply


def never_receives(connection):
    multiprocessing.Event().wait(120)


def startup_probe(connection, root, mode, cancel=False):
    pool = ReusableWorkerPool('test', worker_target=never_receives, revision_reader=lambda: 'test') if mode == 'reused' else None
    try:
        args=([{'code': 'SH.600000', 'name': 'fixture'}], {'frequencies': ['1m']},
              {'1m': {'cutoff': 100, 'large_context': 'x'*2_000_000}}, Path(root), 1, 'test')
        timeout=60 if cancel else 2
        stream=(pool.scan(*args, request={}, timeout_seconds=timeout) if pool is not None
                else bounded_scan(*args, worker_target=never_receives, timeout_seconds=timeout))
        values=[];reported=False
        for result,_ in stream:
            if result is not None:values.append(result)
            elif cancel and not reported:
                connection.send('ready-to-cancel');reported=True
        connection.send(values)
    finally:
        if pool is not None:pool.close()
        connection.close()


@pytest.mark.skipif(os.name != 'nt', reason='Windows named-pipe startup and job-object cleanup')
@pytest.mark.parametrize('mode',['cold','reused'])
def test_large_context_does_not_block_before_worker_timeout_starts(tmp_path,mode):
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe()
    process = context.Process(target=startup_probe, args=(child, str(tmp_path),mode))
    try:
        process.start()
        child.close()
        assert parent.poll(8), 'Large pipe write bypassed the configured worker deadline'
        values = parent.recv()
        assert values[0]['rows'][0]['data_errors'] == ['WORKER_TIMEOUT']
        process.join(timeout=3)
        assert not process.is_alive()
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        process.close()
        parent.close()


@pytest.mark.skipif(os.name != 'nt', reason='Windows named-pipe startup and job-object cleanup')
@pytest.mark.parametrize('mode',['cold','reused'])
def test_large_context_does_not_prevent_cancelling_a_blocked_initializer(tmp_path,mode):
    context=multiprocessing.get_context('spawn');parent,child=context.Pipe()
    process=context.Process(target=startup_probe,args=(child,str(tmp_path),mode,True))
    try:
        process.start();child.close()
        assert parent.poll(8) and parent.recv()=='ready-to-cancel'
        (tmp_path/'cancel').touch()
        assert parent.poll(5) and parent.recv()==[]
        process.join(timeout=3)
        assert not process.is_alive()
    finally:
        if process.is_alive():
            process.terminate();process.join(timeout=5)
        process.close();parent.close()


def make_task(root, *, context=None):
    stock={'market':'a','code':'SH.600000','name':'测试标的','holding':True}
    contexts={'1m':context or {'cutoff':100}}
    reference=store_job([stock],{'frequencies':['1m']},contexts,root,'r',{'force_rebuild':True})
    wire=bounded_message({'ticket':'a'*32+'_0','index':0,'revision':'r','inputs':reference})
    return wire,stock,contexts


def test_large_input_and_reply_keep_control_messages_small_and_values_exact(tmp_path):
    wire,stock,contexts=make_task(tmp_path,context={'cutoff':100,'bars':list(range(200000))})
    assert len(pickle.dumps(wire))<8192
    task=JobInputReader().load(wire)
    assert task['stock']==stock and task['contexts']==contexts
    result={'code':stock['code'],'rows':[],'detail':'图表'*1_000_000}
    response=store_reply(task,result)
    assert len(pickle.dumps(response))<8192
    assert read_reply(response,task['ticket'],tmp_path)==result
    assert Path(response['result_file']['file']).exists()
    assert discard_reply(response,task['ticket'],tmp_path)
    assert not Path(response['result_file']['file']).exists()
    assert Path(wire['inputs']['file']).exists()


def test_control_limit_leaves_room_inside_the_actual_pipe_buffer():
    from multiprocessing.connection import BUFSIZE
    assert MAX_CONTROL_BYTES <= BUFSIZE // 2
    with pytest.raises(ValueError,match='bounded size'):
        bounded_message({'value':'x'*MAX_CONTROL_BYTES})


def test_corrupt_new_job_cannot_fall_back_to_cached_previous_inputs(tmp_path):
    old,_,_=make_task(tmp_path/'old');reader=JobInputReader();reader.load(old)
    new,_,_=make_task(tmp_path/'new',context={'cutoff':200})
    Path(new['inputs']['file']).write_text('{}',encoding='utf-8')
    with pytest.raises(ValueError,match='checksum'):
        reader.load(new)
    assert reader.load(old)['contexts']['1m']['cutoff']==100


@pytest.mark.parametrize('fault',['ticket','directory'])
def test_wrong_reply_is_not_consumed_or_deleted(tmp_path,fault):
    wire,_,_=make_task(tmp_path/'owner');task=JobInputReader().load(wire)
    response=store_reply(task,{'code':'SH.600000','rows':[]})
    path=Path(response['result_file']['file'])
    if fault=='ticket':response['ticket']='b'*32+'_0'
    directory=tmp_path/'other' if fault=='directory' else tmp_path/'owner'
    with pytest.raises(ValueError):read_reply(response,task['ticket'],directory)
    assert path.exists()


def test_valid_reply_survives_a_temporary_cleanup_denial(tmp_path,monkeypatch):
    wire,_,_=make_task(tmp_path);task=JobInputReader().load(wire)
    result={'code':'SH.600000','rows':[]}
    response=store_reply(task,result)
    path=Path(response['result_file']['file'])
    original=Path.unlink
    def locked(self,*args,**kwargs):
        if self==path:raise PermissionError('temporary reply lock')
        return original(self,*args,**kwargs)
    with monkeypatch.context() as fault:
        fault.setattr(Path,'unlink',locked)
        assert read_reply(response,task['ticket'],tmp_path)==result
        assert discard_reply(response,task['ticket'],tmp_path) is False
    assert path.exists()
    assert discard_reply(response,task['ticket'],tmp_path)
    assert not path.exists()


def test_cleanup_preserves_a_reply_changed_after_it_was_read(tmp_path):
    wire,_,_=make_task(tmp_path);task=JobInputReader().load(wire)
    result={'code':'SH.600000','rows':[]};response=store_reply(task,result)
    assert read_reply(response,task['ticket'],tmp_path)==result
    path=Path(response['result_file']['file'])
    path.write_text('{"changed":true}',encoding='utf-8')
    assert discard_reply(response,task['ticket'],tmp_path) is False
    assert path.read_text(encoding='utf-8')=='{"changed":true}'
