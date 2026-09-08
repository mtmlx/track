"""Release a protected-main commit to existing Track schedules, never run writes early."""
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import time

import boto3

REGION = 'us-east-2'
ACCOUNT = '525753067477'
BUCKET = 'track-trace-artifacts-525753067477-us-east-2'
CLUSTER = 'track-trace-prod'
REPO = 'track-trace'
CARRIERS = ('maersk', 'wan-hai', 'cma-cgm', 'msc', 'one')
ROOT = Path(__file__).resolve().parent
EVIDENCE = Path('release-evidence')


def wait_until(read, ready, timeout, interval=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if ready(value):
            return value
        time.sleep(interval)
    raise TimeoutError('Release verification timed out')


def schedule_request(client, snapshot):
    # UpdateSchedule resets omitted optional values: preserve every supported field.
    keys = client.meta.service_model.operation_model('UpdateSchedule').input_shape.members
    return {k: copy.deepcopy(v) for k, v in snapshot.items() if k in keys and k != 'ClientToken'}


def task_request(client, baseline, image, family=None):
    keys = client.meta.service_model.operation_model('RegisterTaskDefinition').input_shape.members
    result = {k: copy.deepcopy(v) for k, v in baseline.items() if k in keys}
    if len(result['containerDefinitions']) != 1:
        raise RuntimeError('Unexpected multi-container task: manual review required')
    result['containerDefinitions'][0]['image'] = image
    if family:
        result['family'] = family
    return result


def promote(scheduler, before, after, save):
    attempted = []
    try:
        for name in before:
            current = schedule_request(scheduler, scheduler.get_schedule(Name=name, GroupName='default'))
            if current != before[name]:
                raise RuntimeError('Schedule changed during release: ' + name)
            attempted.append(name)  # Include ambiguous update responses in recovery.
            scheduler.update_schedule(**after[name])
            verified = schedule_request(scheduler, scheduler.get_schedule(Name=name, GroupName='default'))
            if verified != after[name]:
                raise RuntimeError('Schedule verification failed: ' + name)
    except BaseException:
        recovery = {}
        for name in reversed(attempted):
            try:
                current = schedule_request(scheduler, scheduler.get_schedule(Name=name, GroupName='default'))
                if current == before[name]:
                    recovery[name] = 'already at baseline'
                elif current == after[name]:
                    scheduler.update_schedule(**before[name])
                    restored = schedule_request(scheduler, scheduler.get_schedule(Name=name, GroupName='default'))
                    if restored != before[name]:
                        raise RuntimeError('rollback readback mismatch')
                    recovery[name] = 'restored'
                else:
                    recovery[name] = 'concurrent change: manual recovery required'
            except Exception as exc:
                recovery[name] = 'recovery failed: ' + type(exc).__name__
        save('rollback-result.json', recovery)
        raise


def main():
    sha = os.environ['RELEASE_SHA']
    if not re.fullmatch('[0-9a-f]{40}', sha):
        raise ValueError('Expected immutable commit SHA')
    if os.environ.get('GITHUB_REF') != 'refs/heads/main' or os.environ.get('GITHUB_REPOSITORY') != 'mtmlx/track':
        raise RuntimeError('Only protected mtmlx/track main can release')
    if subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip() != sha:
        raise RuntimeError('Checkout does not match release SHA')
    session = boto3.Session(region_name=REGION)
    if session.client('sts').get_caller_identity()['Account'] != ACCOUNT:
        raise RuntimeError('Wrong AWS account')
    s3, cb, ecr, ecs, scheduler = [session.client(n) for n in ['s3', 'codebuild', 'ecr', 'ecs', 'scheduler']]
    EVIDENCE.mkdir(exist_ok=True)
    release_id = sha + '-' + os.environ['GITHUB_RUN_ID'] + '-' + os.environ.get('GITHUB_RUN_ATTEMPT', '1')
    def save(name, data):
        body = json.dumps(data, indent=2, default=str)
        (EVIDENCE / name).write_text(body)
        s3.put_object(Bucket=BUCKET, Key=f'github-releases/{release_id}/{name}', Body=body.encode(), ServerSideEncryption='AES256')
    source = EVIDENCE / 'source.zip'
    subprocess.run(['git', 'archive', '--format=zip', '-o', str(source), sha], check=True)
    key = f'github-releases/{release_id}/source.zip'
    s3.upload_file(str(source), BUCKET, key, ExtraArgs={'ServerSideEncryption': 'AES256'})
    source.unlink()  # Evidence artifact need not duplicate all source code.
    tag = 'github-' + release_id
    build = cb.start_build(projectName='track-trace-image-build', sourceLocationOverride=BUCKET+'/'+key,
        buildspecOverride=(ROOT/'buildspec.json').read_text(), environmentVariablesOverride=[
            {'name':'IMAGE_TAG','value':tag,'type':'PLAINTEXT'},
            {'name':'COMMIT_TAG','value':sha,'type':'PLAINTEXT'}])['build']
    save('build.json', {'id':build['id'], 'commit':sha})
    result = wait_until(lambda: cb.batch_get_builds(ids=[build['id']])['builds'][0],
                        lambda b: b['buildStatus'] != 'IN_PROGRESS', 2400)
    save('build-result.json', {'status':result['buildStatus'], 'logs':result.get('logs')})
    if result['buildStatus'] != 'SUCCEEDED':
        raise RuntimeError('Build/tests/dependency audit failed')
    digest = ecr.describe_images(repositoryName=REPO, imageIds=[{'imageTag':tag}])['imageDetails'][0]['imageDigest']
    def scan():
        try:
            return ecr.describe_image_scan_findings(repositoryName=REPO, imageId={'imageDigest':digest})
        except ecr.exceptions.ScanNotFoundException:
            return {'imageScanStatus':{'status':'IN_PROGRESS'}}
    scanned = wait_until(scan, lambda x: x['imageScanStatus']['status'] != 'IN_PROGRESS', 600)
    save('image-scan.json', scanned)
    if scanned['imageScanStatus']['status'] != 'COMPLETE':
        raise RuntimeError('Image scan unavailable')
    counts = scanned['imageScanFindings'].get('findingSeverityCounts', {})
    if counts.get('CRITICAL', 0) or counts.get('HIGH', 0):
        raise RuntimeError('Image has critical/high findings')
    image = f'{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{REPO}@{digest}'
    before, definitions = {}, {}
    for carrier in CARRIERS:
        name = f'track-trace-{carrier}-every-6h'
        snap = scheduler.get_schedule(Name=name, GroupName='default')
        if snap['State'] != 'ENABLED' or snap['Target']['Arn'] != f'arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/{CLUSTER}':
            raise RuntimeError('Unexpected schedule scope/state')
        before[name] = schedule_request(scheduler, snap)
        td = ecs.describe_task_definition(taskDefinition=snap['Target']['EcsParameters']['TaskDefinitionArn'])['taskDefinition']
        if td['family'] != f'track-trace-{carrier}':
            raise RuntimeError('Unexpected task family')
        definitions[name] = td
    save('rollback-schedules.json', before)
    # No production schedule changed before this live read-only integration check.
    sample_name = 'track-trace-maersk-every-6h'
    pilot = task_request(ecs, definitions[sample_name], image, 'track-trace-release-pilot')
    container = pilot['containerDefinitions'][0]
    container['command'] = ['python', '-c', (ROOT/'read_only_pilot.py').read_text()]
    overrides = {'CLICKUP_USE_TASK_STATUS':'false', 'SHIPMENT_ALLOWED_LINES':'maersk',
                 'SHIPMENT_COMMENT_ON_NO_CHANGE':'false', 'SHIPMENT_AUDIT_SOURCE':'release-read-only-pilot'}
    container['environment'] = [v for v in container.get('environment',[]) if v['name'] not in overrides] + [{'name':k,'value':v} for k,v in overrides.items()]
    container['secrets'] = [v for v in container.get('secrets',[]) if v['name'] not in overrides]
    td_arn = ecs.register_task_definition(**pilot)['taskDefinition']['taskDefinitionArn']
    net = before[sample_name]['Target']['EcsParameters']['NetworkConfiguration']['awsvpcConfiguration']
    net = {k[0].lower()+k[1:]:v for k,v in net.items()}
    run = ecs.run_task(cluster=CLUSTER, taskDefinition=td_arn, launchType='FARGATE',
                       networkConfiguration={'awsvpcConfiguration':net}, count=1, startedBy='github-track-release')
    if run.get('failures') or len(run.get('tasks',[])) != 1:
        raise RuntimeError('Pilot failed to start')
    task = run['tasks'][0]['taskArn']
    save('pilot.json', {'taskArn':task})
    try:
        done = wait_until(lambda: ecs.describe_tasks(cluster=CLUSTER,tasks=[task])['tasks'][0], lambda t:t['lastStatus']=='STOPPED', 600)
    except BaseException:
        ecs.stop_task(cluster=CLUSTER, task=task, reason='Release pilot timed out or interrupted')
        raise
    save('pilot-result.json', {'taskArn':task, 'containers':done['containers'], 'stoppedReason':done.get('stoppedReason')})
    if any(c.get('exitCode') != 0 for c in done['containers']):
        raise RuntimeError('Read-only pilot failed or had no candidates')
    after = copy.deepcopy(before)
    for name, baseline in definitions.items():
        new = ecs.register_task_definition(**task_request(ecs, baseline, image))['taskDefinition']['taskDefinitionArn']
        after[name]['Target']['EcsParameters']['TaskDefinitionArn'] = new
    save('planned-schedules.json', after)
    promote(scheduler, before, after, save)
    save('release.json', {'commit':sha,'image':image,'schedules':after,'status':'schedule targets verified',
                          'note':'Workers execute on their existing schedules; production write results are not yet verified.'})
    print('Verified ECS schedule targets for', sha, digest)


if __name__ == '__main__':
    main()
