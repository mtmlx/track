"""Safe, actionable shipment notifications; raw exceptions stay in operational logs."""
from dataclasses import dataclass
import re
from urllib.parse import quote

from shipment_sync.models import ShipmentRef


@dataclass(frozen=True)
class FailureNotice:
    category: str
    issue: str
    reason: str
    actions: tuple[str, ...]


def carrier_name(value: str) -> str:
    key = re.sub(r'[^a-z0-9]', '', value.lower())
    groups = {
        'ONE': ('one', 'oceannetworkexpress'),
        'MSC': ('msc', 'mscshippingline', 'mediterraneanshippingcompany'),
        'Maersk': ('maersk', 'maerskline', 'apmollermaersk'),
        'CMA CGM': ('cmacgm',),
        'Wan Hai': ('wanhai', 'wanhailines', 'whl'),
        'COSCO': ('cosco', 'coscoshipping', 'coscoshippingline', 'coscoshippinglines'),
        'Hapag-Lloyd': ('hapaglloyd', 'hapaglloydag'),
        'Evergreen': ('evergreen', 'evergreenline', 'evergreenmarine'),
        'PIL': ('pil', 'pacificinternationallines'),
        'OOCL': ('oocl', 'orientoverseascontainerline'),
    }
    return next((name for name, aliases in groups.items() if key in aliases), 'Carrier')


def source_url(shipment: ShipmentRef) -> str | None:
    carrier = carrier_name(shipment.shipping_line)
    if carrier == 'ONE':
        reference = shipment.booking_no or shipment.container_no or ''
        return ('https://ecomm.one-line.com/one-ecom/manage-shipment/cargo-tracking?trakNoParam='
                + quote(reference, safe='') + ('&trakNoTpCdParam=B' if shipment.booking_no else '&trakNoTpCdParam=C'))
    return {
        'MSC': 'https://www.msc.com/en/track-a-shipment',
        'Maersk': 'https://www.maersk.com/tracking/',
        'CMA CGM': 'https://www.cma-cgm.com/ebusiness/tracking',
        'Wan Hai': 'https://www.wanhai.com/views/quick/cargo_tracking.xhtml',
        'COSCO': 'https://elines.coscoshipping.com/ebusiness/cargoTracking',
        'Hapag-Lloyd': 'https://www.hapag-lloyd.com/en/online-business/track/track-by-container-solution.html',
        'Evergreen': 'https://www.evergreen-line.com/',
        'PIL': 'https://www.pilship.com/en-our-track-and-trace-p.html',
        'OOCL': 'https://www.oocl.com/eng/ourservices/eservices/cargotracking/Pages/cargotracking.aspx',
    }.get(carrier)


def classify_failure(shipment: ShipmentRef, error: str, *, phase: str = 'lookup') -> FailureNotice:
    message = error.lower()
    if phase == 'writeback':
        return FailureNotice('writeback_uncertain',
            'Tracking reached ClickUp writeback, but the update did not complete reliably.',
            'Some fields or comments may already have been saved. An automatic replay could duplicate or overwrite work.',
            ('Ask integration support to compare current ClickUp fields, activity, and the run logs before retrying.',
             'Reply here with any confirmed differences; do not reset fields or force a status to bypass the failure.'))
    if phase == 'preflight' or any(s in message for s in (
            'adapter not configured', 'no adapter registered', 'missing credentials', 'missing api',
            'http 401', 'http 403', '401 client error', '403 client error', 'unauthorized',
            'invalid_client', 'authentication', 'oauth', 'token request')):
        return FailureNotice('integration_access',
            'The carrier integration could not establish a usable connection or authorized API access.',
            'This is a connection/configuration issue, not proof that the booking is invalid or cancelled.',
            ('Ask integration support to check the carrier endpoint, DNS/network access, credentials, and customer/API permissions. Never post credentials in this task.',
             'Confirm the booking reference against carrier evidence, but do not change valid shipment references to work around an access problem.'))
    if any(s in message for s in ('anti-bot', 'antibot', 'captcha', 'access denied', 'blocked',
                                  'challenge', 'incapsula', 'query form not available')):
        actions = [
            'Open the official carrier link in a normal authorized browser and search the booking and each assigned container. An access block does not mean the booking is invalid.',
            'If accessible, attach the complete tracking result, including all container histories and destination details, with the capture time. If still blocked, report that to integration support or the carrier; do not repeatedly retry the blocked service.',
        ]
        if carrier_name(shipment.shipping_line) == 'Wan Hai':
            actions.append('Wan Hai Manual Capture needed: copy and paste the entire result screen/page into this task, include screenshots of all details, and mention TracyAI for the approved manual extraction workflow.')
        return FailureNotice('carrier_blocked', 'The carrier blocked or challenged automated/browser access.',
            'No reliable result was obtained through this access path. Changing shipment data cannot resolve an anti-bot restriction.', tuple(actions))
    if any(s in message for s in ('destination', 'port of discharge', 'pod ', 'port evidence')):
        return FailureNotice('destination_evidence',
            'The shipment destination and carrier event-location evidence are missing, ambiguous, or inconsistent.',
            'A transshipment discharge cannot be treated as arrival at the final destination without matching port evidence.',
            ('Verify the task POD against the carrier booking/bill of lading and record the authoritative destination port/code.',
             'Provide the full event history and locations to integration support. Correct only evidence-supported destination references; do not guess an arrival or discharge date.'))
    if any(s in message for s in ('conflict', 'identity mismatch', 'journey mismatch', 'unexpected container',
                                  'does not include', 'lacks matching booking', 'does not contain that container')):
        return FailureNotice('identity_conflict',
            'The carrier booking/container response conflicts with this shipment, or matching identity evidence is missing.',
            'We cannot safely establish that the response belongs to the booking and containers recorded here.',
            ('Confirm the booking and container allocation with the carrier or booking party, including any split or reassignment.',
             'Correct only the affected references using confirmed evidence. If the task is correct, ask the carrier to correct or clarify its records. Do not guess a replacement reference or force a shipment status.'))
    if any(s in message for s in ('validated containers', 'container population', 'missing booking',
            'missing container', 'requires a container', 'needs a booking', 'invalid container',
            'capture(s)', 'single container', 'container number', 'container-level', 'unambiguous shipment booking')):
        return FailureNotice('missing_references',
            'The lookup does not have a complete, usable booking/container reference set or per-container evidence.',
            'A quantity or placeholder is not a container identity. Container-level tracking requires the correct assigned units and complete evidence for each.',
            ('Verify the latest booking and allocated containers against the carrier confirmation or bill of lading. Container numbers must have four letters followed by seven digits.',
             'Enter only confirmed identifiers and verify Number of Containers against the allocation. If not assigned, leave the container-number field genuinely blank rather than entering "Not Assigned". Booking-only tracking depends on explicit carrier confirmation and support in that carrier adapter.',
             'If the confirmed allocation exceeds an integration limit, ask integration support to handle the complete shipment; do not remove real containers or reduce the quantity to bypass the limit.',
             'For browser capture/import, include a separate complete result for every assigned container. Do not import a partial batch as a complete shipment.'))
    if any(s in message for s in ('no result', 'no events', 'no usable', 'unavailable or incomplete',
            'no explicit lifecycle', 'ambiguous confirmation', 'dated tracking events', 'invalid event list',
            'pagination', 'no identity', 'timezone', 'timestamp', 'classifier', 'in the future', 'returned empty')):
        return FailureNotice('carrier_data_incomplete',
            'The carrier returned no usable result or insufficient/ambiguous tracking evidence for a safe update.',
            'The failed lookup alone does not establish that the booking is invalid, cancelled, or unconfirmed. Missing container numbers alone do not disqualify a supported, explicitly confirmed booking-only lookup.',
            ('Verify the reference and current booking status with the carrier/booking party and check whether it was replaced or split. Correct a reference only when supporting evidence confirms the correction.',
             'If the website shows details but the integration does not, attach the complete result/screenshot and ask integration support to review the API or capture. If the carrier has no complete record, request carrier confirmation; do not manufacture dates or milestones.'))
    if any(s in message for s in ('timeout', 'timed out', '429', 'rate limit', 'connection',
                                  'http 500', 'http 502', 'http 503', 'http 504')):
        return FailureNotice('service_unavailable', 'A timeout, service error, or request limit prevented tracking.',
            'The service did not return a reliable result. This is not evidence of an invalid booking.',
            ('Ask integration support to check carrier availability and retry/backoff limits. Avoid repeated manual retries while the service is failing.',
             'Use the official carrier link to obtain current evidence when available; keep valid booking/container references unchanged.'))
    return FailureNotice('technical_review', 'A technical or validation failure prevented a reliable tracking update.',
        'The failure does not prove that shipment references or status are incorrect. Detailed diagnostics remain in the operational logs.',
        ('Ask integration support to review this task and its failed run before retrying; do not guess missing data or reset fields.',
         'Share the complete carrier result and capture time here, together with any confirmed booking/container correction.'))


def failure_comment(shipment: ShipmentRef, notice: FailureNotice, *, method: str, phase: str,
                    reference: str | None = None) -> str:
    lines = [f'{carrier_name(shipment.shipping_line)} Track & Trace - action required',
             f'Path: {method}; failure stage: {phase}',
             f'Booking checked: {shipment.booking_no or "not set"}',
             f'Containers recorded: {shipment.container_no or "not assigned"}',
             f'Expected container count: {shipment.expected_container_count or "not set"}']
    if reference:
        lines.append(f'Reference checked: {reference}')
    url = source_url(shipment)
    if url:
        lines.append('Carrier source: ' + url)
    lines += ['', 'ISSUE: ' + notice.issue, '',
              'IMPACT: Tracking dates, vessel/voyage, container details, and status may remain stale or incomplete. Using unsupported data could affect customer visibility, shipment documents, and manifests.',
              '', 'WHY WE CANNOT PROCEED: ' + notice.reason]
    if phase == 'not_queried':
        lines.append('This shipment was not queried: an earlier carrier access failure opened the circuit breaker for this run. It is not a shipment-specific rejection.')
    elif phase == 'preflight':
        lines.append('This shipment was not queried because the carrier connection preflight failed.')
    if phase == 'writeback':
        lines.append('Write outcome is uncertain: current ClickUp values must be verified before retrying. This notification does not change shipment fields or status.')
    else:
        lines.append('This failed lookup did not write shipment fields, Last T&T Update, or workflow status. This notification only adds a comment.')
    lines += ['', 'CORRECTIVE ACTION - TASK ASSIGNEE:']
    lines.extend(f'{i}. {action}' for i, action in enumerate(notice.actions, 1))
    lines += [f'{len(notice.actions) + 1}. Reply here with the confirmed references/status, supporting source, any correction made, and what remains blocked. Once resolved, rerun T&T or allow the next scheduled run; tracking resumes only when the carrier response passes the shipment identity checks and required validation.',
              'Please share the confirmed details here so we have one reliable record. If confirmation is pending, let us know what is missing so we can resolve the blocker together.']
    return '\n'.join(lines)
