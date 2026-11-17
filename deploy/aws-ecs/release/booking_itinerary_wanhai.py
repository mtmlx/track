"""Narrow reviewed Wan Hai native-evidence release; never runs carrier workers.

prepare is local + AWS metadata reads. build uploads immutable source and starts
one existing CodeBuild job. status reads progress. plan creates review artifacts.
deploy requires the exact plan digest, preserves the existing schedule and all
other task settings, and refuses automatic recovery of an unknown outcome.
"""
import argparse,copy,hashlib,json,re,shlex,subprocess,zipfile
from datetime import datetime,timezone,timedelta
from pathlib import Path
ACCOUNT='525753067477';REGION='us-east-2';SCHEDULE='track-trace-wan-hai-every-6h'
CLUSTER=f'arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/track-trace-prod'
ARTIFACT_BUCKET='track-trace-artifacts-525753067477-us-east-2'
BUCKET='logjson-staging-bucket-saa6dyocsgeh';TENANT='b99e1c1281a18ff33519f4fd8079b397e0b27e3bc5e35b39b796782ed0791987'
PREFIX=f'customers/{TENANT}/workflow/carrier-movements';CATALOG=PREFIX+'/booking-itinerary-catalog.json'
TASKS={'86e3fe0j3','86e3fe0j2','86e3vj9xp'} # 33810 / 33804 / 34845; no carrier inference.
ROLE='TrackTraceTaskRole';POLICY='MovesaWanHaiBookingEvidenceRead'
CRM_FIELD='5e1637c2-6bb2-4428-a592-06477fd13ef1'
NEW_FILES={'src/shipment_sync/booking_itinerary_archive.py','src/shipment_sync/booking_itinerary_html.py','tests/test_booking_itinerary_archive.py','deploy/aws-ecs/release/booking_itinerary_wanhai.py','tests/test_booking_itinerary_release.py'}
CHANGED_FILES=NEW_FILES|{'src/shipment_sync/models.py','src/shipment_sync/sync.py','src/shipment_sync/carriers/wan_hai.py','src/shipment_sync/carriers/cma_cgm.py','requirements-os.lock'}
GST_PATCH={
 'gstreamer1.0-plugins-base':'fca3bd01ab67475dc6b763787175d156b69c807701f8f84b867b58dd46615380',
 'libgstreamer-gl1.0-0':'c937a6e6cf1b1435a929fe7d436aa281b976cf7033bec15582c30d0d7094eaea',
 'libgstreamer-plugins-base1.0-0':'2048dad3bc852f82d00353d60cd5be0d013172179955263588c2ea191e81870d'}
ROOT=Path(__file__).resolve().parents[3]
def encode(x):return json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=False,default=str).encode()
def sha(x):return hashlib.sha256(encode(x)).hexdigest()
def save(out,name,x):
 path=out/name;path.write_bytes(encode(x));path.chmod(0o600)
def read(out,name):return json.loads((out/name).read_bytes())
def request(client,operation,snapshot):
 allowed=client.meta.service_model.operation_model(operation).input_shape.members
 return {k:copy.deepcopy(v)for k,v in snapshot.items()if k in allowed and k!='ClientToken'}
def clients():
 import boto3
 from botocore.config import Config
 s=boto3.Session(region_name=REGION);opts=Config(connect_timeout=5,read_timeout=30,retries={'total_max_attempts':1})
 if s.client('sts',config=opts).get_caller_identity()['Account']!=ACCOUNT:raise ValueError('Wrong AWS account')
 c={n:s.client(n,config=opts)for n in ('s3','codebuild','ecr','ecs','scheduler','iam')};c['data']=s.client('s3',region_name='us-east-1',config=opts)
 return c

def overlay_dockerfile(base_image):
 if not re.fullmatch(r'525753067477\.dkr\.ecr\.us-east-2\.amazonaws\.com/track-trace@sha256:[a-f0-9]{64}',base_image):raise ValueError('Pinned deployed base image required')
 packages=sorted(GST_PATCH);version='1.28.2-1ubuntu0.3';old='1.28.2-1ubuntu0.2'
 lock_check="from pathlib import Path; parse=lambda p:dict(line.split('=',1)for line in Path(p).read_text().splitlines()if line and not line.startswith('#')); before=parse('/app/requirements-os.lock'); after=parse('/tmp/booking-build-locks/requirements-os.lock'); changes={k:(before.get(k),after.get(k))for k in set(before)|set(after)if before.get(k)!=after.get(k)}; assert changes=="+repr({p:(old,version)for p in packages})
 installed_check="from pathlib import Path; parse=lambda p:dict(line.split('=',1)for line in Path(p).read_text().splitlines()if line); before=parse('/tmp/booking-dpkg-before'); after=parse('/tmp/booking-dpkg-after'); changes={k.split(':')[0]:(before.get(k),after.get(k))for k in set(before)|set(after)if before.get(k)!=after.get(k)}; assert changes=="+repr({p:(old,version)for p in packages})
 files=[p+'_'+version+'_amd64.deb'for p in packages]
 checks='\\n'.join(GST_PATCH[p]+'  '+filename for p,filename in zip(packages,files))+'\\n'
 patch=('RUN python -c '+shlex.quote(lock_check)+' && dpkg-query -W -f=\'${binary:Package}=${Version}\\n\' > /tmp/booking-dpkg-before\n'
  'RUN set -eu; mkdir /tmp/gstreamer-security; cd /tmp/gstreamer-security; apt-get update; apt-get download '+ ' '.join(p+'='+version for p in packages)+'; '
  'printf %b '+shlex.quote(checks)+' | sha256sum -c -; apt-get install --only-upgrade --no-install-recommends --no-remove -y '+' '.join('./'+f for f in files)+'; '
  'dpkg-query -W -f=\'${binary:Package}=${Version}\\n\' > /tmp/booking-dpkg-after; python -c '+shlex.quote(installed_check)+'; '
  'cp /tmp/booking-build-locks/requirements-os.lock /app/requirements-os.lock; rm -rf /var/lib/apt/lists/* /tmp/gstreamer-security\n')
 return (f'FROM {base_image}\nUSER root\nWORKDIR /app\n'
  'COPY requirements.lock build-requirements.lock requirements-os.lock pyproject.toml /tmp/booking-build-locks/\n'
  'RUN set -eu; for name in requirements.lock build-requirements.lock pyproject.toml; do cmp "/app/$name" "/tmp/booking-build-locks/$name"; done\n'+patch+
  'COPY src /app/src\nCOPY scripts /app/scripts\n'
  'RUN python -m pip install --no-deps --no-build-isolation /app && python -m pip check && chown -R appuser:appuser /app && chmod +x /app/scripts/run_sync.sh /app/scripts/run_sync_loop.sh\n'
  'USER appuser\n').encode()

def source(out,base_image):
 status=subprocess.check_output(['git','diff','--name-only'],cwd=ROOT,text=True).splitlines()
 if set(status)-CHANGED_FILES:raise ValueError('Unreviewed source changes outside this release')
 names=set(subprocess.check_output(['git','ls-files','-z'],cwd=ROOT).decode().split('\0'))-{''}
 names|={n for n in NEW_FILES if (ROOT/n).is_file()}
 generated={'Dockerfile.booking-itinerary':overlay_dockerfile(base_image)}
 files={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest()for n in sorted(names)}
 files.update({n:hashlib.sha256(data).hexdigest()for n,data in generated.items()})
 manifest={'base_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),'files':files,'base_image':base_image}
 release=sha(manifest);save(out,'source-manifest.json',manifest)
 with zipfile.ZipFile(out/'source.zip','w',compression=zipfile.ZIP_DEFLATED)as z:
  for n in sorted(names|set(generated)):
   data=generated[n]if n in generated else(ROOT/n).read_bytes()
   if hashlib.sha256(data).hexdigest()!=files[n]:raise ValueError('Source changed during packaging')
   info=zipfile.ZipInfo(n,(2026,1,1,0,0,0));info.external_attr=((0o755 if n not in generated and (ROOT/n).stat().st_mode&0o111 else 0o644)<<16);info.compress_type=zipfile.ZIP_DEFLATED;z.writestr(info,data)
 return release

def baseline(c):
 schedule=request(c['scheduler'],'UpdateSchedule',c['scheduler'].get_schedule(Name=SCHEDULE,GroupName='default'))
 if schedule['State']!='ENABLED'or schedule['Target']['Arn']!=CLUSTER:raise ValueError('Unexpected Wan Hai schedule')
 td=c['ecs'].describe_task_definition(taskDefinition=schedule['Target']['EcsParameters']['TaskDefinitionArn'])['taskDefinition']
 if td['family']!='track-trace-wan-hai'or len(td['containerDefinitions'])!=1 or td.get('taskRoleArn')!=f'arn:aws:iam::{ACCOUNT}:role/{ROLE}':raise ValueError('Unexpected worker identity')
 return schedule,request(c['ecs'],'RegisterTaskDefinition',td)

def archive_env(release):
 return {'BOOKING_ITINERARY_ARCHIVE_ENABLED':'true','BOOKING_ITINERARY_ARCHIVE_REGION':'us-east-1','BOOKING_ITINERARY_ARCHIVE_BUCKET':BUCKET,
         'BOOKING_ITINERARY_ARCHIVE_PREFIX':PREFIX,'BOOKING_ITINERARY_ARCHIVE_CATALOG_KEY':CATALOG,'BOOKING_ITINERARY_ARCHIVE_BUCKET_OWNER':ACCOUNT,'BOOKING_ITINERARY_ARCHIVE_RELEASE_SHA256':release}

def task_candidate(base,image,release):
 result=copy.deepcopy(base);container=result['containerDefinitions'][0];env=archive_env(release)
 if any(v['name']in env for v in container.get('secrets',[])):raise ValueError('Archive names unexpectedly overlap secrets')
 container['image']=image;container['environment']=[v for v in container.get('environment',[])if v['name']not in env]+[{'name':k,'value':v}for k,v in env.items()]
 return result

def canonical_task(value):
 # ECS may reorder named environment/secret entries on native readback. Their
 # values remain exact; duplicate names are ambiguous and never normalized away.
 result=copy.deepcopy(value)
 for container in result.get('containerDefinitions',[]):
  for field in ('environment','secrets'):
   if field not in container:continue
   entries=container[field];names=[v['name']for v in entries]
   if len(names)!=len(set(names)):raise ValueError('Duplicate task configuration names')
   container[field]=sorted(entries,key=lambda v:v['name'])
 return result

def narrow_policy():
 # Existing reviewed GetObject/PutObject feed grants already match these child
 # paths. Add only new catalog read and exact version reads, without replacement.
 return {'Version':'2012-10-17','Statement':[
  {'Sid':'ReadReviewedWanHaiBookingCatalog','Effect':'Allow','Action':['s3:GetObject'],'Resource':['arn:aws:s3:::'+BUCKET+'/'+CATALOG]},
  {'Sid':'ReadExactWanHaiBookingVersions','Effect':'Allow','Action':['s3:GetObjectVersion'],'Resource':['arn:aws:s3:::'+BUCKET+'/'+PREFIX+'/'+task+'/booking-itinerary/manifests/*.json'for task in sorted(TASKS)]}]}

def prepare(c,out):
 schedule,td=baseline(c);release=source(out,td['containerDefinitions'][0]['image'])
 save(out,'baseline.json',{'schedule':schedule,'task':td,'source_release_sha256':release,'source_zip_sha256':hashlib.sha256((out/'source.zip').read_bytes()).hexdigest()})
 save(out,'iam-addition.json',narrow_policy())
 return {'state':'PREPARED','source_release_sha256':release,'schedule':SCHEDULE,'source_changes_only':True}

def build(c,out):
 base=read(out,'baseline.json');release=base['source_release_sha256'];prior=out/'build.json'
 if prior.exists():return read(out,'build.json')
 data=(out/'source.zip').read_bytes()
 if hashlib.sha256(data).hexdigest()!=base['source_zip_sha256']:raise ValueError('Source archive changed')
 key='booking-itinerary-releases/'+release+'/source.zip'
 try:c['s3'].put_object(Bucket=ARTIFACT_BUCKET,Key=key,Body=data,ServerSideEncryption='AES256',ExpectedBucketOwner=ACCOUNT,IfNoneMatch='*')
 except Exception as e:
  if getattr(e,'response',{}).get('Error',{}).get('Code')!='PreconditionFailed':raise
 verify=c['s3'].get_object(Bucket=ARTIFACT_BUCKET,Key=key,ExpectedBucketOwner=ACCOUNT)['Body'].read(len(data)+1)
 if verify!=data:raise ValueError('Native build source readback mismatch')
 tag='booking-itinerary-'+release[:24]
 save(out,'build.json',{'state':'REQUEST_STARTED','release':release,'tag':tag,'idempotency_token':release})
 buildspec=json.loads((ROOT/'deploy/aws-ecs/release/buildspec.json').read_text())
 buildspec['phases']['build']['commands'][0]='docker build -f Dockerfile.booking-itinerary -t \"$REPOSITORY_URI:$IMAGE_TAG\" .'
 result=c['codebuild'].start_build(projectName='track-trace-image-build',sourceLocationOverride=ARTIFACT_BUCKET+'/'+key,
  buildspecOverride=json.dumps(buildspec),idempotencyToken=release,
  environmentVariablesOverride=[{'name':'IMAGE_TAG','value':tag,'type':'PLAINTEXT'},{'name':'COMMIT_TAG','value':base['source_zip_sha256'],'type':'PLAINTEXT'}])['build']
 state={'state':'BUILD_STARTED','id':result['id'],'release':release,'tag':tag};save(out,'build.json',state);return state

def status(c,out):
 state=read(out,'build.json')
 if not state.get('id'):raise ValueError('Unknown build-call outcome; reconcile by release token, do not replay')
 b=c['codebuild'].batch_get_builds(ids=[state['id']])['builds'][0];result={'state':b['buildStatus'],'build_id':b['id'],'logs':b.get('logs')}
 if b['buildStatus']=='SUCCEEDED':
  image=c['ecr'].describe_images(repositoryName='track-trace',imageIds=[{'imageTag':state['tag']}])['imageDetails'][0]
  scan=c['ecr'].describe_image_scan_findings(repositoryName='track-trace',imageId={'imageDigest':image['imageDigest']});counts=scan.get('imageScanFindings',{}).get('findingSeverityCounts',{})
  if scan['imageScanStatus']['status']!='COMPLETE'or counts.get('HIGH')or counts.get('CRITICAL'):raise ValueError('Image vulnerability scan not cleared')
  result['image']=f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/track-trace@{image['imageDigest']}"
 save(out,'build-status.json',result);return result

def make_plan(c,out,catalog_file):
 base=read(out,'baseline.json');image=read(out,'build-status.json')['image'];schedule,td=baseline(c)
 if schedule!=base['schedule']or td!=base['task']:raise ValueError('Worker changed since baseline')
 catalog=json.loads(Path(catalog_file).read_bytes());now=datetime.now(timezone.utc)
 if (catalog.get('reader_id')!='tracktrace-booking-itinerary-v1'or catalog.get('binding',{}).get('tenant')!=TENANT or
     catalog.get('crm_field_id')!=CRM_FIELD or
     catalog.get('reader_release_sha256')!=base['source_release_sha256']or set(catalog.get('tasks',{}))!=TASKS):raise ValueError('Exact reviewed three-task Wan Hai catalog required')
 for task,row in catalog['tasks'].items():
  when=datetime.fromisoformat(row['native_scope_verified_at'].replace('Z','+00:00'))
  if not timedelta(0)<=now-when<=timedelta(hours=1):raise ValueError('Fresh native task/customer/carrier/booking binding required')
  binding=row['binding']
  if binding.get('task_id')!=task or binding.get('carrier')!='WAN HAI'or binding.get('tenant')!=TENANT or binding.get('coverage_kind')!='BOOKING_ITINERARY':raise ValueError('Catalog outside reviewed scope')
 previous=None
 try:
  r=c['data'].get_object(Bucket=BUCKET,Key=CATALOG,ExpectedBucketOwner=ACCOUNT);previous={'etag':r['ETag'],'sha256':hashlib.sha256(r['Body'].read(1_000_001)).hexdigest()}
 except Exception as e:
  if getattr(e,'response',{}).get('Error',{}).get('Code')!='NoSuchKey':raise
 policy_before=None
 try:policy_before=c['iam'].get_role_policy(RoleName=ROLE,PolicyName=POLICY)['PolicyDocument']
 except c['iam'].exceptions.NoSuchEntityException:pass
 if policy_before not in (None,narrow_policy()):raise ValueError('Existing unrelated policy must not be replaced')
 plan={'baseline':base,'catalog':catalog,'catalog_before':previous,'iam_addition':narrow_policy(),'iam_before':policy_before,
       'task_candidate':task_candidate(td,image,base['source_release_sha256']),'schedule_unchanged_except_task_revision':True,
       'carrier_queries':0,'source_field_writes':0,'other_schedules_changed':0}
 save(out,'plan.json',plan);return {'state':'READY_FOR_REVIEW','plan_sha256':sha(plan),'tasks':sorted(TASKS),'schedule_expression':schedule['ScheduleExpression']}

def deploy(c,out,reviewed):
 plan=read(out,'plan.json')
 if sha(plan)!=reviewed:raise ValueError('Reviewed plan digest required')
 if (out/'execution.json').exists():raise ValueError('Prior execution requires native reconciliation; never replay blindly')
 schedule,td=baseline(c)
 if schedule!=plan['baseline']['schedule']or td!=plan['baseline']['task']:raise ValueError('Concurrent worker/schedule change')
 current_policy=None
 try:current_policy=c['iam'].get_role_policy(RoleName=ROLE,PolicyName=POLICY)['PolicyDocument']
 except c['iam'].exceptions.NoSuchEntityException:pass
 if current_policy!=plan['iam_before']:raise ValueError('Concurrent IAM policy change')
 for row in plan['catalog']['tasks'].values():
  verified=datetime.fromisoformat(row['native_scope_verified_at'].replace('Z','+00:00'))
  if not timedelta(0)<=datetime.now(timezone.utc)-verified<=timedelta(hours=1):raise ValueError('Native catalog scope must be refreshed before deployment')
 state={'state':'STARTED','plan_sha256':reviewed};save(out,'execution.json',state)
 c['iam'].put_role_policy(RoleName=ROLE,PolicyName=POLICY,PolicyDocument=json.dumps(plan['iam_addition']))
 if c['iam'].get_role_policy(RoleName=ROLE,PolicyName=POLICY)['PolicyDocument']!=plan['iam_addition']:raise ValueError('IAM readback mismatch')
 args={'Bucket':BUCKET,'Key':CATALOG,'Body':encode(plan['catalog']),'ContentType':'application/json','ServerSideEncryption':'AES256','ExpectedBucketOwner':ACCOUNT}
 if plan['catalog_before']:args['IfMatch']=plan['catalog_before']['etag']
 else:args['IfNoneMatch']='*'
 response=c['data'].put_object(**args);state['catalog_version']=response.get('VersionId');save(out,'execution.json',state)
 saved=c['data'].get_object(Bucket=BUCKET,Key=CATALOG,ExpectedBucketOwner=ACCOUNT)['Body'].read(1_000_001)
 if saved!=encode(plan['catalog']):raise ValueError('Catalog readback mismatch')
 # Register only once. If the response is lost, use the unique image/environment
 # fingerprint to reconcile native task definitions rather than retrying.
 state['state']='TASK_REGISTRATION_STARTED';save(out,'execution.json',state)
 arn=c['ecs'].register_task_definition(**plan['task_candidate'])['taskDefinition']['taskDefinitionArn'];state['task_definition']=arn;save(out,'execution.json',state)
 actual=request(c['ecs'],'RegisterTaskDefinition',c['ecs'].describe_task_definition(taskDefinition=arn)['taskDefinition'])
 if canonical_task(actual)!=canonical_task(plan['task_candidate']):raise ValueError('Task definition readback mismatch')
 if request(c['scheduler'],'UpdateSchedule',c['scheduler'].get_schedule(Name=SCHEDULE,GroupName='default'))!=schedule:raise ValueError('Concurrent schedule change')
 after=copy.deepcopy(schedule);after['Target']['EcsParameters']['TaskDefinitionArn']=arn
 state['state']='SCHEDULE_UPDATE_STARTED';save(out,'execution.json',state);c['scheduler'].update_schedule(**after)
 if request(c['scheduler'],'UpdateSchedule',c['scheduler'].get_schedule(Name=SCHEDULE,GroupName='default'))!=after:raise ValueError('Schedule readback mismatch')
 state.update(state='DEPLOYED_SCHEDULE_VERIFIED',carrier_queries=0,source_field_writes=0,native_publisher_outcome='NOT_YET_OBSERVED');save(out,'execution.json',state);return state

def main():
 p=argparse.ArgumentParser();p.add_argument('stage',choices=['prepare','build','status','plan','deploy']);p.add_argument('--out',required=True);p.add_argument('--catalog');p.add_argument('--reviewed-plan-sha256');a=p.parse_args()
 out=Path(a.out);out.mkdir(parents=True,exist_ok=True,mode=0o700);c=clients()
 result={'prepare':lambda:prepare(c,out),'build':lambda:build(c,out),'status':lambda:status(c,out),'plan':lambda:make_plan(c,out,a.catalog),'deploy':lambda:deploy(c,out,a.reviewed_plan_sha256)}[a.stage]()
 print(json.dumps(result,indent=2,default=str))
if __name__=='__main__':main()
