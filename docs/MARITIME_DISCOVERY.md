# Maritime Track & Trace discovery

The default list-name include patterns are `shipment,maritimo`. Matching is
case- and accent-insensitive, so `Maritimos` and `Marítimos` both match.
Existing explicit exclusions take precedence. A matching list must still pass
the tracking-field schema check; carrier filters, terminal-status exclusions,
reference validation, destination validation, and writeback guards are unchanged.

Production configuration must also set
`CLICKUP_DISCOVERY_LIST_NAME_INCLUDE=shipment,maritimo` because an explicit
environment value overrides the default. The release script sets this non-secret
value in every new carrier task definition, removing only an old binding for the
same name. Shared credentials are not rewritten. Keep the existing space boundaries.

Discovery caches carry a fingerprint of the discovery scope and required field
IDs. Changing the include/exclude patterns or scope invalidates the cache,
including stale fallback. Legacy caches without a fingerprint are refreshed.

## October 16, 2026 audit

A read-only enumeration of both configured spaces found two non-archived
maritime lists excluded by the former `shipment` filter. Both passed the live
required-field schema check:

| List ID | Name | Space ID |
| --- | --- | --- |
| 901714461043 | Marítimos Rones de Guatemala | 90171126576 |
| 901717124562 | Marítimos Polindustrias | 90171126576 |

No additional maritime-named lists were found in space 90171139728.
Terrestrial and administrative lists remain excluded by the name filter.
This audit validates discovery eligibility, not every task's carrier references.

Canary: `MTMLXGT-26408` / `86e1qk2x1`, booking `276508515`, container
`TRHU8425865`, POD Puerto Quetzal. Before applying, fetch fresh carrier evidence
and preview the exact task. The October 16 public tracking page showed estimated
destination arrival November 1, 2026 at 16:00 local and final leg
`MAERSK GLACIER / 643S`. An intermediate arrival at Lazaro Cardenas is not final
destination arrival. Verify the actual saved ETA/vessel and retained transit
status after the canary; the document does not itself assert successful repair.
