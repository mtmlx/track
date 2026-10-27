"""Deployment failure recovery must not erase unrelated schedule changes."""
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import subprocess
import pytest

spec = importlib.util.spec_from_file_location('track_release', Path(__file__).parents[1] / 'deploy/aws-ecs/release/release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_iam_drift_stops_release_before_any_aws_write(monkeypatch, tmp_path):
    sha = 'a' * 40
    monkeypatch.setenv('RELEASE_SHA', sha)
    monkeypatch.setenv('GITHUB_REF', 'refs/heads/main')
    monkeypatch.setenv('GITHUB_REPOSITORY', 'mtmlx/track')
    monkeypatch.setattr(release.subprocess, 'check_output', lambda *a, **k: sha)
    client = Mock()
    client.get_caller_identity.return_value = {'Account': release.ACCOUNT}
    session = Mock()
    session.client.return_value = client
    monkeypatch.setattr(release.boto3, 'Session', lambda **k: session)
    monkeypatch.setattr(release, 'EVIDENCE', tmp_path)
    commands = []
    def reject_guard(command, **kwargs):
        commands.append(command)
        raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(release.subprocess, 'run', reject_guard)
    with pytest.raises(subprocess.CalledProcessError):
        release.main()
    assert len(commands) == 1
    assert commands[0][1] == str(release.ROOT.parents[2] / 'scripts/check_cloudwatch_iam.py')
    assert '--live' in commands[0]
    assert [call[0] for call in client.method_calls] == ['get_caller_identity']

class Scheduler:
    def __init__(self, state, fail=None, concurrent=False):
        self.state = copy.deepcopy(state)
        self.fail = fail
        self.concurrent = concurrent
        shape = SimpleNamespace(members={'Name':None,'Target':None,'State':None,'Description':None})
        self.meta = SimpleNamespace(service_model=SimpleNamespace(operation_model=lambda _:SimpleNamespace(input_shape=shape)))
    def get_schedule(self, Name, GroupName):
        return copy.deepcopy(self.state[Name])
    def update_schedule(self, **request):
        self.state[request['Name']] = copy.deepcopy(request)
        if request['Name'] == self.fail:
            self.fail = None
            if self.concurrent:
                self.state[request['Name']]['Description'] = 'operator changed this'
            raise TimeoutError('ambiguous response after applied update')

@pytest.fixture
def snapshots():
    before = {n:{'Name':n,'Target':{'revision':1},'State':'ENABLED','Description':'keep'} for n in ['a','b']}
    after = copy.deepcopy(before)
    for v in after.values(): v['Target']['revision'] = 2
    return before,after

def test_success_preserves_optional_settings(snapshots):
    before,after = snapshots
    c = Scheduler(before)
    release.promote(c,before,after,lambda *_:None)
    assert c.state == after

def test_ambiguous_write_rolls_back_all_attempted_targets(snapshots):
    before,after = snapshots
    c = Scheduler(before,fail='b')
    reports=[]
    with pytest.raises(TimeoutError):release.promote(c,before,after,lambda *x:reports.append(x))
    assert c.state == before
    assert reports[0][1] == {'b':'restored','a':'restored'}

def test_concurrent_operator_change_is_not_overwritten(snapshots):
    before,after = snapshots
    c = Scheduler(before,fail='b',concurrent=True)
    reports=[]
    with pytest.raises(TimeoutError):release.promote(c,before,after,lambda *x:reports.append(x))
    assert c.state['a'] == before['a']
    assert c.state['b']['Description'] == 'operator changed this'
    assert 'manual recovery' in reports[0][1]['b']

def test_preexisting_drift_stops_before_write(snapshots):
    before,after = snapshots
    c = Scheduler(before)
    c.state['a']['State'] = 'DISABLED'
    with pytest.raises(RuntimeError,match='changed during'):release.promote(c,before,after,lambda *_:None)
    assert c.state['b'] == before['b']


def test_task_release_versions_maritime_scope_without_changing_credentials():
    baseline = {'family':'track-trace-maersk', 'containerDefinitions':[{
        'name':'worker', 'image':'old',
        'environment':[{'name':'SHIPMENT_ALLOWED_LINES','value':'maersk'}],
        'secrets':[{'name':'CLICKUP_DISCOVERY_LIST_NAME_INCLUDE','valueFrom':'old-config'},
                   {'name':'CLICKUP_API_TOKEN','valueFrom':'keep-token'}],
    }]}
    before = copy.deepcopy(baseline)
    shape = SimpleNamespace(members={'family':None,'containerDefinitions':None})
    client = SimpleNamespace(meta=SimpleNamespace(service_model=SimpleNamespace(operation_model=lambda _:SimpleNamespace(input_shape=shape))))
    result = release.task_request(client, baseline, 'new-digest')
    container = result['containerDefinitions'][0]
    assert container['image'] == 'new-digest'
    assert container['environment'] == [
        {'name':'SHIPMENT_ALLOWED_LINES','value':'maersk'},
        {'name':'CLICKUP_DISCOVERY_LIST_NAME_INCLUDE','value':'shipment,maritimo'},
    ]
    assert container['secrets'] == [{'name':'CLICKUP_API_TOKEN','valueFrom':'keep-token'}]
    assert baseline == before
