"""Deployment failure recovery must not erase unrelated schedule changes."""
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import pytest

spec = importlib.util.spec_from_file_location('track_release', Path(__file__).parents[1] / 'deploy/aws-ecs/release/release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

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
