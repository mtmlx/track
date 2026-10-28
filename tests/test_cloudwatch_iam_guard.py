import copy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('guard', ROOT / 'scripts/check_cloudwatch_iam.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class CloudWatchPreservation(unittest.TestCase):
    def setUp(self):
        self.docs = {label: json.loads((guard.BASELINES / (label + '-policy.json')).read_text()) for label in guard.TARGETS}

    def test_current_manifests_pass(self):
        self.assertEqual(guard.check()['canonical'], 'passed')

    def test_old_arn_only_replacement_rejected(self):
        for label, baseline in self.docs.items():
            old = copy.deepcopy(baseline)
            old['Statement'] = [s for s in old['Statement'] if not s.get('Sid', '').endswith('LogGroupArnCompatibility')]
            with self.assertRaises(ValueError):
                guard.require_preserved(label, old, baseline)

    def test_stream_and_nonlogging_permissions_cannot_disappear(self):
        for label, baseline in self.docs.items():
            for index in range(len(baseline['Statement'])):
                changed = copy.deepcopy(baseline)
                changed['Statement'].pop(index)
                with self.assertRaises(ValueError):
                    guard.require_preserved(label, changed, baseline)

    def test_new_deny_rejected(self):
        doc = copy.deepcopy(self.docs['sso'])
        doc['Statement'].append({'Effect': 'Deny', 'Action': 'logs:*', 'Resource': '*'})
        with self.assertRaises(ValueError):
            guard.require_preserved('sso', doc, self.docs['sso'])

    def test_broadened_manifest_rejected(self):
        doc = copy.deepcopy(self.docs['sso'])
        doc['Statement'][-1]['Resource'] = ['*']
        with self.assertRaises(ValueError):
            guard.validate_baseline('sso', doc)

    def test_order_only_changes_are_allowed(self):
        doc = copy.deepcopy(self.docs['cfo'])
        doc['Statement'].reverse()
        for statement in doc['Statement']:
            if isinstance(statement.get('Action'), list):
                statement['Action'].reverse()
        guard.require_preserved('cfo', doc, self.docs['cfo'])

    def test_live_verification_reads_both_roles_and_identity_center(self):
        calls = []
        def fake(*args):
            calls.append(args)
            if args[0] == 'sts':
                return {'Account': guard.ACCOUNT}
            if args[0] == 'sso-admin':
                return {'InlinePolicy': json.dumps(self.docs['sso'])}
            label = 'cfo' if args[3] == guard.TARGETS['cfo'][0] else 'sso'
            return {'PolicyDocument': self.docs[label]}
        with patch.object(guard, 'aws', side_effect=fake):
            report = guard.check(live=True, identity_center=True)
        self.assertEqual(len(report['live_roles']), 2)
        self.assertEqual(report['identity_center'], 'passed')
        self.assertEqual({(c[0], c[1]) for c in calls}, {
            ('sts', 'get-caller-identity'), ('iam', 'get-role-policy'),
            ('sso-admin', 'get-inline-policy-for-permission-set')})

    def test_wrong_account_stops_before_policy_reads(self):
        with patch.object(guard, 'aws', return_value={'Account': '000000000000'}) as aws:
            with self.assertRaises(ValueError):
                guard.check(live=True)
            self.assertEqual(aws.call_count, 1)


if __name__ == '__main__':
    unittest.main()
