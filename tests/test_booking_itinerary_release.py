from copy import deepcopy
import importlib.util
from pathlib import Path
spec=importlib.util.spec_from_file_location('release',Path(__file__).resolve().parents[1]/'deploy/aws-ecs/release/booking_itinerary_wanhai.py');release=importlib.util.module_from_spec(spec);spec.loader.exec_module(release)

def test_candidate_preserves_source_permissions_command_roles_and_every_other_setting():
 baseline={'family':'track-trace-wan-hai','taskRoleArn':'existing-task-role','executionRoleArn':'existing-execution-role',
  'containerDefinitions':[{'name':'worker','image':'old','command':['shipment-sync'],'environment':[{'name':'SHIPMENT_ALLOWED_LINES','value':'wan hai'},
   {'name':'MOVEMENT_ARCHIVE_ENABLED','value':'true'},{'name':'EXISTING_HOLD','value':'preserved'}],'secrets':[{'name':'CLICKUP_API_TOKEN','valueFrom':'existing-secret'}]}]}
 before=deepcopy(baseline);candidate=release.task_candidate(baseline,'new-pinned-image','a'*64)
 assert baseline==before
 restored=deepcopy(candidate);restored['containerDefinitions'][0]['image']='old'
 restored['containerDefinitions'][0]['environment']=[v for v in restored['containerDefinitions'][0]['environment']if not v['name'].startswith('BOOKING_ITINERARY_ARCHIVE_')]
 assert restored==baseline
 assert all(v['value']=='true'for v in candidate['containerDefinitions'][0]['environment']if v['name']=='BOOKING_ITINERARY_ARCHIVE_ENABLED')

def test_additive_policy_only_adds_catalog_and_exact_versions_for_three_verified_shipments():
 policy=release.narrow_policy();assert len(policy['Statement'])==2
 assert policy['Statement'][0]['Action']==['s3:GetObject']
 assert policy['Statement'][1]['Action']==['s3:GetObjectVersion']
 resources=policy['Statement'][1]['Resource'];assert len(resources)==3
 assert all('/booking-itinerary/manifests/*.json'in x for x in resources)
 assert all(any('/'+task+'/'in x for task in release.TASKS)for x in resources)
 assert len(release.TASKS)==3

def test_source_overlay_uses_pinned_deployed_base_and_enforces_identical_dependency_locks():
 import pytest
 image='525753067477.dkr.ecr.us-east-2.amazonaws.com/track-trace@sha256:'+'a'*64
 text=release.overlay_dockerfile(image).decode();assert text.startswith('FROM '+image+'\n')
 assert 'cmp "/app/$name" "/tmp/booking-build-locks/$name"'in text
 assert '--no-deps --no-build-isolation'in text and 'pip check'in text
 assert 'apt-get upgrade'not in text and text.endswith('USER appuser\n')
 assert 'sha256sum -c -'in text and '--no-remove'in text
 assert 'assert changes=='in text
 for name,sha in release.GST_PATCH.items():
  assert name+'=1.28.2-1ubuntu0.3'in text and sha in text
 assert all(line.startswith(('FROM ','USER ','WORKDIR ','COPY ','RUN '))for line in text.splitlines())
 with pytest.raises(ValueError):release.overlay_dockerfile('unreviewed:latest')

def test_native_readback_allows_named_entry_order_only():
 candidate={'containerDefinitions':[{'name':'worker','command':['shipment-sync'],'environment':[
  {'name':'ZETA','value':'unchanged'},{'name':'ALPHA','value':'true'}],
  'secrets':[{'name':'ZETA_SECRET','valueFrom':'exact-z'},{'name':'ALPHA_SECRET','valueFrom':'exact-a'}]}]}
 actual=deepcopy(candidate);actual['containerDefinitions'][0]['environment'].reverse();actual['containerDefinitions'][0]['secrets'].reverse()
 assert release.canonical_task(actual)==release.canonical_task(candidate)
 actual['containerDefinitions'][0]['environment'][0]['value']='false'
 assert release.canonical_task(actual)!=release.canonical_task(candidate)
 actual=deepcopy(candidate);actual['containerDefinitions'][0]['command']=['other-command']
 assert release.canonical_task(actual)!=release.canonical_task(candidate)

def test_duplicate_configuration_names_fail_instead_of_disappearing():
 import pytest
 value={'containerDefinitions':[{'environment':[{'name':'ONE','value':'1'},{'name':'ONE','value':'2'}]}]}
 with pytest.raises(ValueError,match='Duplicate'):release.canonical_task(value)

def test_generated_checksum_printf_produces_three_real_lines():
 import shlex,subprocess
 image='525753067477.dkr.ecr.us-east-2.amazonaws.com/track-trace@sha256:'+'a'*64
 text=release.overlay_dockerfile(image).decode()
 line=next(line for line in text.splitlines()if 'printf %b 'in line)
 tokens=shlex.split(line);index=tokens.index('printf')
 result=subprocess.run(['/usr/bin/printf',tokens[index+1],tokens[index+2]],check=True,capture_output=True)
 expected=[release.GST_PATCH[name]+'  '+name+'_1.28.2-1ubuntu0.3_amd64.deb'for name in sorted(release.GST_PATCH)]
 assert result.stdout.decode().splitlines()==expected
 assert result.stdout.count(b'\n')==3 and b'\\n'not in result.stdout
