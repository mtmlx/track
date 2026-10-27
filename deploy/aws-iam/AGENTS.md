# Preserve reviewed operational access

For IAM or Identity Center policy changes in account 525753067477, preserve the
CloudWatch migration in cloudwatch-compatibility and all original policy statements.
Never replace an AWSReservedSSO role policy directly; update its permission set,
provision it to the account, and verify both source and generated role.

Before writing a proposed CFO or invoice-operator replacement, run:

    python scripts/check_cloudwatch_iam.py --candidate cfo --policy PATH
    python scripts/check_cloudwatch_iam.py --candidate sso --policy PATH

After an authorized IAM deployment, run:

    python scripts/check_cloudwatch_iam.py --live --identity-center

A failed check requires review; do not remove permissions, broaden access, change
workloads, disable schedules, or rewrite AWS-owned service-linked roles to pass it.
See docs/CLOUDWATCH_IAM_COMPATIBILITY.md for ownership, verification, and rollback.
