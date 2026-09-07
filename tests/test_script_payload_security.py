import json
import subprocess
import sys
import requests
import pytest
from shipment_sync.carriers.common import extract_json_from_http_response


def response(body, content_type='text/html'):
    r=requests.Response();r._content_consumed=True;r.status_code=200;r._content=body.encode();r.headers['Content-Type']=content_type
    return r


@pytest.mark.parametrize('body,expected', [
 ('<script>{"early":true}</script><SCRIPT ID = "__NEXT_DATA__">{"next":true}</SCRIPT>', {'next':True}),
 ('<script>bad</script><script>{invalid}</script><script>[1,2]</script>', {'data':[1,2]}),
 ('<script data-id="__NEXT_DATA__">{"generic":true}</script>', {'generic':True}),
 ('<script>{"text":"a &amp; b"}</script>', {'text':'a &amp; b'}),
 ('<script id="__NEXT_DATA__"/>{"next":true}</script>', {'next':True}),
 ('<script/>{"ok":true}</script>', {'ok':True}),
])
def test_script_payloads_keep_json_semantics(body,expected):
    assert extract_json_from_http_response(response(body))==expected


def test_invalid_next_data_does_not_fall_back():
    with pytest.raises(json.JSONDecodeError):
        extract_json_from_http_response(response('<script>{"ok":true}</script><script id="__NEXT_DATA__">invalid</script>'))


def test_json_content_type_preserved():
    assert extract_json_from_http_response(response('[1]', 'application/json')) == {'data':[1]}


@pytest.mark.parametrize('marker',['<script>','<script id="__NEXT_DATA__">','<script','</'])
def test_malformed_script_input_has_bounded_runtime(marker):
    code='''import requests,sys
from shipment_sync.carriers.common import extract_json_from_http_response
r=requests.Response();r._content_consumed=True;r._content=(sys.argv[1]*50000).encode();r.headers['Content-Type']='text/html'
try: extract_json_from_http_response(r)
except ValueError: pass
else: raise AssertionError('unterminated script accepted')
'''
    subprocess.run([sys.executable,'-c',code,marker],check=True,timeout=5)
