import gzip
import json
import math
import random
import struct

import pytest

from chanlun.screening import evidence


def canonical(value):
    return json.dumps(value,ensure_ascii=True,sort_keys=True,separators=(',',':'))


@pytest.mark.parametrize('raw', [
    b'{"id":"2026-10-30T13:05:00+08:00","x":null,"b":true,"a":[1,2.5]}',
    b'{"a":1,"a":2,"b":-0.0}',
    b'[18446744073709551616,-340282366920938463463374607431768211456]',
    ('['+str(10**500)+']').encode(),
    b'[NaN,Infinity,-Infinity,1e999,-1e-999]',
    b'{"text":"\\ud800","nul":"\\u0000"}',
    '{"中文":"标签😀"}'.encode('utf-8'),
    b'\xef\xbb\xbf{"x":1}',
    '{"x":123}'.encode('utf-16'),
    '{"x":123}'.encode('utf-32'),
])
def test_values_and_python_types_match_original_json(raw):
    assert evidence._fast_json_decode is not None
    assert canonical(evidence._snapshot_json(raw))==canonical(json.loads(raw))


def test_float_bits_and_large_integer_types_are_exact():
    rng=random.Random(5013)
    values=[]
    for _ in range(12000):
        value=struct.unpack('!d',rng.getrandbits(64).to_bytes(8,'big'))[0]
        if math.isfinite(value):values.append(value)
    raw=json.dumps(values,separators=(',',':')).encode()
    actual=evidence._snapshot_json(raw)
    assert [struct.pack('!d',x) for x in actual]==[struct.pack('!d',x) for x in values]
    huge=evidence._snapshot_json(str(10**500).encode())
    assert type(huge) is int and huge==10**500


@pytest.mark.parametrize('raw',[b'{broken',b'[1,]',b'{"x":1}//comment',b'{}{}',b'{"x":"\x00"}',b'\xff',b'-NaN',b'01'])
def test_invalid_input_is_not_relaxed(raw):
    with pytest.raises((ValueError,UnicodeError)):
        json.loads(raw)
    with pytest.raises((ValueError,UnicodeError)):
        evidence._snapshot_json(raw)


def test_python_integer_conversion_limit_is_preserved():
    raw=b'9'*10000
    try:expected=json.loads(raw)
    except ValueError:
        with pytest.raises(ValueError):evidence._snapshot_json(raw)
    else:assert evidence._snapshot_json(raw)==expected


def test_missing_optional_decoder_preserves_original_path(monkeypatch):
    monkeypatch.setattr(evidence,'_fast_json_decode',None)
    assert evidence._snapshot_json(b'{"x":2.5}')=={'x':2.5}


def test_valid_snapshot_uses_fast_decoder_and_original_structure_checks(monkeypatch):
    original=evidence._fast_json_decode;calls=[]
    def counted(raw):calls.append(raw);return original(raw)
    monkeypatch.setattr(evidence,'_fast_json_decode',counted)
    value={'symbol':'SH.600000','source_frequency':'5m','levels':[]}
    assert evidence._parse_snapshot(gzip.compress(json.dumps(value).encode()))==value
    assert len(calls)==1
    with pytest.raises(ValueError,match='格式无效'):
        evidence._parse_snapshot(gzip.compress(b'{"symbol":"x","source_frequency":"5m","levels":{}}'))
