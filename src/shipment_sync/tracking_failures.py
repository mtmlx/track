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
    details: tuple[str, ...] = ()


def _msc_validation_notice(shipment: ShipmentRef, error: str) -> FailureNotice | None:
    """Expose known validation facts, never arbitrary exception text or URLs."""
    message = error.lower()
    if message.startswith('msc booking search returned no results found'):
        return FailureNotice('msc_reference_not_found',
            'MSC returned "No results found" for the booking shown above.',
            'This does not establish cancellation or an invalid booking. Without a matching carrier record we cannot validate dates, containers or workflow status.',
            ('Ask the booking party to confirm the carrier-issued booking and current Master B/L, including any rebooking or split.',
             'Record the confirmed Master B/L and allocated containers, then check those exact references on the MSC website. If MSC has not published the record, request confirmation rather than inventing identifiers.'),
            ('Fallback references: ' + ('Master B/L is recorded.' if shipment.master_bl_no else 'Master B/L is not recorded.')
             + (' Containers are recorded.' if shipment.container_no else ' No containers are recorded.'),))
    if 'msc container count conflict:' in message or ('clickup has ' in message and 'number of containers' in message):
        count = len(set(re.findall(r'\b[A-Z]{4}\d{7}\b', shipment.container_no or '', re.I)))
        return FailureNotice('msc_container_count_conflict',
            'The recorded container list disagrees with Number of Containers for this shipment.',
            'A shared booking may contain separate B/L allocations. Combining those allocations could put another shipment\'s units and milestones into this task.',
            ('Compare this task\'s exact Master B/L against MSC and the booking-party confirmation. Confirm which units belong to this B/L.',
             'Correct only the proven allocation or quantity. Do not remove real units, reduce quantity or force a status just to bypass validation.'),
            (f'Recorded unique containers: {count}; expected quantity: {shipment.expected_container_count if shipment.expected_container_count is not None else "not set"}.',))
    if message.startswith('msc b/l identity conflict:'):
        return FailureNotice('msc_bl_identity_conflict',
            'The carrier capture has a missing, different or ambiguous Master B/L allocation.',
            'A matching booking alone cannot authorize importing a different B/L from a shared booking.',
            ('Check the Master B/L recorded on this task against the carrier confirmation and visible MSC result.',
             'Capture the exact recorded B/L, with a complete history for each assigned container. Ask integration support to review conflicting results; never edit or stitch carrier headers.'))
    if message.startswith('msc browser review is incomplete:'):
        details = []
        for label in ('missing', 'unexpected'):
            match = re.search(re.escape(label) + r' container capture\(s\): ([A-Z0-9, ]+)', error)
            refs = sorted(set(re.findall(r'\b[A-Z]{4}\d{7}\b', match.group(1)))) if match else []
            if refs:
                details.append(f'{label.capitalize()} container evidence: ' + ', '.join(refs[:25]) + '.')
        binding = 'binding limitation' in message
        return FailureNotice('msc_bl_capture_incomplete' if binding else 'msc_container_coverage',
            'The booking capture does not cover the full recorded container allocation.',
            ('The Master B/L result contains the units but the importer has not validated that B/L binding. This is an integration evidence gap, not proof that a container is invalid.'
             if binding else 'We cannot safely combine missing or unexpected units with this shipment. Individual container searches alone do not prove booking/B/L ownership.'),
            ('Search the recorded Master B/L and each listed container on MSC; preserve the original B/L header, POD, delivery place, timestamp and full histories.',
             'Provide the full result to integration support for a validated B/L import. If the allocation differs, obtain carrier confirmation before changing task references. Do not delete containers or fabricate headers.'), tuple(details))
    if ('destination/pod is missing' in message or message.startswith('msc journey mismatch:')
            and not shipment.destination_port):
        return FailureNotice('msc_destination_missing',
            'The authoritative shipment POD is blank in ClickUp.',
            'The carrier may show a route, but tracking cannot assume that its discharge port is the destination intended for this task.',
            ('Confirm the destination port/code against the shipment booking or B/L and populate the task POD.',
             'Keep ocean POD separate from inland delivery. Do not substitute a transshipment port or guess arrival/discharge dates.'))
    return None


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
        if not reference:
            return 'https://ecomm.one-line.com/one-ecom/manage-shipment/cargo-tracking'
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
    if phase == 'references':
        return FailureNotice('missing_references',
            'Both Booking number and Container(s) number(s) are blank, so tracking has no shipment reference to query.',
            'No carrier lookup or tracking field/status update was attempted. Last T&T Update is not refreshed; dates and customer notices must not be treated as newly validated.',
            ('If the booking has been issued, enter its confirmed carrier booking reference and attach the confirmation here. A supported confirmed booking can be checked before containers are assigned.',
             'Enter assigned container numbers only when confirmed by carrier evidence. Do not invent a reference or force a status to start tracking.',
             'If booking is still pending, keep the task pending and reply here with the booking owner and expected confirmation date. Correcting the reference will make the task eligible for the next tracking run.'))
    if phase == 'partial_browser_batch':
        failed = classify_failure(shipment, error)
        return FailureNotice('partial_browser_batch_' + failed.category,
            'Some browser evidence was accepted for this task, but another reference failed. ' + failed.issue,
            'This is a carrier/capture failure, not evidence that an accepted ClickUp write failed. Tracking coverage is still incomplete. ' + failed.reason,
            failed.actions)
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
    if carrier_name(shipment.shipping_line) == 'MSC':
        specific = _msc_validation_notice(shipment, error)
        if specific is not None:
            return specific
    if any(s in message for s in ('destination', 'port of discharge', 'pod ', 'port evidence')):
        return FailureNotice('destination_evidence',
            'The shipment destination and carrier event-location evidence are missing, ambiguous, or inconsistent.',
            'A transshipment discharge cannot be treated as arrival at the final destination without matching port evidence.',
            ('Verify the task POD against the carrier booking/bill of lading and record the authoritative destination port/code.',
             'Provide the full event history and locations to integration support. Correct only evidence-supported destination references; do not guess an arrival or discharge date.'))
    if 'conflicting origin departure schedules' in message:
        return FailureNotice('booking_schedule_conflict',
            'ONE returned conflicting departure dates or sailings for the same origin port.',
            'We cannot choose a reliable ETD or reconcile the rebooking. Using the old date or guessing a replacement could trigger an incorrect status or customer departure notice.',
            ('Ask the carrier/booking party to confirm the current booking, first-origin departure date, vessel/export voyage and cutoff together; attach that confirmation here.',
             'Ask integration support to reconcile the conflicting carrier schedules before rerunning. Do not change container identities or force transit to bypass this hold.',
             'Do not send a departure notice from forecast ETD alone; actual qualifying carrier movement evidence is required.'))
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
    if carrier_name(shipment.shipping_line) == 'MSC':
        lines.append('Recorded Master B/L: ' + (shipment.master_bl_no or 'not set'))
        lines.append('Shipment POD: ' + (shipment.destination_port or 'not set'))
    url = source_url(shipment)
    if url:
        lines.append('Carrier source: ' + url)
    lines += ['', 'ISSUE: ' + notice.issue, '',
              'IMPACT: Tracking dates, vessel/voyage, container details, and status may remain stale or incomplete. Using unsupported data could affect customer visibility, shipment documents, and manifests.',
              '', 'WHY WE CANNOT PROCEED: ' + notice.reason]
    if notice.details:
        lines += ['', 'VALIDATION DETAILS:', *notice.details]
    if phase == 'not_queried':
        lines.append('This shipment was not queried: an earlier carrier access failure opened the circuit breaker for this run. It is not a shipment-specific rejection.')
    elif phase == 'preflight':
        lines.append('This shipment was not queried because the carrier connection preflight failed.')
    if phase == 'writeback':
        lines.append('Write outcome is uncertain: current ClickUp values must be verified before retrying. This notification does not change shipment fields or status.')
    elif phase == 'partial_browser_batch':
        lines.append('Accepted updates are not being rolled back. The failed reference is not treated as complete shipment evidence. No automatic replay or reset will occur.')
    else:
        lines.append('This failed lookup did not write shipment fields, Last T&T Update, or workflow status. This notification only adds a comment.')
    lines += ['', 'CORRECTIVE ACTION - TASK ASSIGNEE:']
    lines.extend(f'{i}. {action}' for i, action in enumerate(notice.actions, 1))
    lines += [f'{len(notice.actions) + 1}. Reply here with the confirmed references/status, supporting source, any correction made, and what remains blocked. Once resolved, rerun T&T or allow the next scheduled run; tracking resumes only when the carrier response passes the shipment identity checks and required validation.',
              'Please share the confirmed details here so we have one reliable record. If confirmation is pending, let us know what is missing so we can resolve the blocker together.']
    return '\n'.join(lines)
