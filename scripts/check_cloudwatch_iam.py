"""Read-only deployment guard for the reviewed 2027 CloudWatch ARN migration.

Default: validate canonical documents. --live: verify both deployed role policies.
--identity-center: also verify the authoritative permission-set source.
--candidate cfo|sso --policy FILE: validate a proposed replacement before a write.
No IAM writes, automatic repairs, or workload invocations are performed.
"""
import argparse
import json
from pathlib import Path
import subprocess

ACCOUNT = '525753067477'
INSTANCE = 'arn:aws:sso:::instance/ssoins-7223aa5b898b3761'
PERMISSION_SET = 'arn:aws:sso:::permissionSet/ssoins-7223aa5b898b3761/ps-72239aac459db18d'
ROOT = Path(__file__).resolve().parents[1]
BASELINES = ROOT / 'deploy/aws-iam/cloudwatch-compatibility'
TARGETS = {
    'cfo': ('MTMLx_CFO_Recovery', 'MTMLxCFORecoveryRuntime', 'ReadInvoiceServiceLogs'),
    'sso': ('AWSReservedSSO_MTMInvoiceOperatorTamara_fda5a47055297712', 'AwsSSOInlinePolicy', 'InvoiceServiceLogs'),
}


def aws(*args):
    return json.loads(subprocess.check_output(['aws', *args, '--output', 'json'], text=True))


def normalize(value):
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return sorted((normalize(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    return value


def validate_baseline(label, policy):
    """Check the migration's essential scope even if someone edits its manifest."""
    sid = TARGETS[label][2]
    original = next(s for s in policy['Statement'] if s.get('Sid') == sid)
    extra = next(s for s in policy['Statement'] if s.get('Sid') == sid + 'LogGroupArnCompatibility')
    actions = {'logs:DescribeLogStreams', 'logs:FilterLogEvents'}
    if label == 'cfo':
        actions.add('logs:DescribeLogGroups')
    prefix = f'arn:aws:logs:us-east-1:{ACCOUNT}:log-group:/aws/elasticbeanstalk/mtm-gt-invoice-webhook-prod'
    groups = {prefix, prefix + '/*'}
    if (extra.get('Effect') != 'Allow' or set(extra.get('Action', [])) != actions
            or set(extra.get('Resource', [])) != groups or extra.get('Condition')):
        raise ValueError(label + ': incompatible or broadened log-group migration statement')
    if (original.get('Effect') != 'Allow' or set(original.get('Action', [])) != actions | {'logs:GetLogEvents'}
            or set(original.get('Resource', [])) != {r + ':*' for r in groups}
            or original.get('Condition')):
        raise ValueError(label + ': original stream access changed')


def require_preserved(label, candidate, baseline):
    if candidate.get('Version') != baseline.get('Version'):
        raise ValueError(label + ': policy version changed; review required')
    statements = [normalize(s) for s in candidate.get('Statement', [])]
    expected = [normalize(s) for s in baseline['Statement']]
    if any(s not in statements for s in expected):
        raise ValueError(label + ': a reviewed permission/condition is missing or changed')
    if any(s.get('Effect') == 'Deny' and s not in expected for s in statements):
        raise ValueError(label + ': new Deny requires effective-permission review')


def check(live=False, identity_center=False, candidate=None, policy_path=None):
    baselines = {label: json.loads((BASELINES / (label + '-policy.json')).read_text()) for label in TARGETS}
    for label, doc in baselines.items():
        validate_baseline(label, doc)
    report = {'account': ACCOUNT, 'canonical': 'passed', 'live_roles': {}, 'identity_center': 'not requested'}
    if candidate:
        require_preserved(candidate, json.loads(Path(policy_path).read_text()), baselines[candidate])
        report['candidate'] = {candidate: 'passed'}
    if live or identity_center:
        if aws('sts', 'get-caller-identity')['Account'] != ACCOUNT:
            raise ValueError('Wrong AWS account; refusing verification')
    if live:
        for label, (role, name, _) in TARGETS.items():
            current = aws('iam', 'get-role-policy', '--role-name', role, '--policy-name', name)['PolicyDocument']
            require_preserved(label, current, baselines[label])
            report['live_roles'][role] = 'passed'
    if identity_center:
        current = json.loads(aws('sso-admin', 'get-inline-policy-for-permission-set', '--instance-arn', INSTANCE,
                                 '--permission-set-arn', PERMISSION_SET, '--region', 'us-east-1')['InlinePolicy'])
        require_preserved('sso', current, baselines['sso'])
        report['identity_center'] = 'passed'
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--identity-center', action='store_true')
    parser.add_argument('--candidate', choices=TARGETS)
    parser.add_argument('--policy', type=Path)
    parser.add_argument('--evidence', type=Path)
    args = parser.parse_args()
    if bool(args.candidate) != bool(args.policy):
        parser.error('--candidate and --policy must be supplied together')
    try:
        report = check(args.live, args.identity_center, args.candidate, args.policy)
    except Exception as exc:
        if args.evidence:
            args.evidence.parent.mkdir(parents=True, exist_ok=True)
            args.evidence.write_text(json.dumps({'status': 'failed', 'error_type': type(exc).__name__}) + '\n')
        raise
    if args.evidence:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
