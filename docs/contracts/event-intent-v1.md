# LLM event interpretation v1

Canonical schema: `event-intent-v1.schema.json`, ID `urn:herdr:event-intent:v1`.
Shared conformance data: `tests/fixtures/event_coordinator/event-intent-v1.json`.
Main prototype owns the only runtime JavaScript validator, gateway request and UI;
event lane owns this schema/examples. There is no event Python runtime validator or
network client for this result, and the existing fixture CLI/report contract is unchanged.

## Exact shape

Required keys: adults, children, childrenAgeRange, budgetMinor, currency,
separateBills, separateBillPayers, dateTime, timezone, clarifications, evidence.
No extra properties are accepted anywhere. Evidence has exactly the first nine
field keys; values are null for unknowns, otherwise nonempty verbatim request quotes.
Clarifications are `{field, reason}`, with a field among those nine and reason
missing/ambiguous/conflicting/unsupported. Code supplies human-readable UI questions.

The model extracts data, never scope, user/task IDs, total guests, credentials,
actions, model policy or grants. A structured response still carries no authority.
Quotes provide provenance for review; a quote does not prove a semantically correct
interpretation. Prompt injection cannot convert extracted data to a command.

Unknown values remain null. Missing children is not zero; missing billing is not
false. Explicit no children maps to zero with evidence. Explicit combined bill can
map to false; unspecified payers must remain null unless separateBills is true.
Payers may be adults/everyone/null. Runtime derives guest total only when counts
are known; it never silently substitutes fixture defaults.

Counts: integers 0–100; known combined counts cannot exceed 100. Child ages: integer
minimum/maximum 0–17, ordered. budgetMinor is a nonnegative safe integer; a currency
is an explicit ISO code, and a dollar symbol alone does not establish USD. The
schema checks the uppercase three-letter syntax only; actual supported currency
codes/minor-unit rules belong to runtime policy. Unsupported or ambiguous currency
requires clarification, not conversion/spending authority. The fixture with $400
keeps budgetMinor 40000 and currency null to preserve the stated dollar amount;
that value cannot become a usable monetary budget until currency is resolved.

Dates/timezones retain explicitly supplied text, including relative dates. The model
must not invent a year, timezone or resolved date; request-context resolution requires
separate trusted runtime logic. Conflicting extracted fields stay null with a
conflicting clarification while independently known fields remain intact.

## Schema versus semantic checks

The JSON Schema enforces closed shape, primitive bounds, payer dependency, and
null/non-null evidence presence. Main's runtime must additionally enforce:

- Each non-null evidence quote occurs verbatim in this exact request (no cross-task
  retrieval or prior request context unless explicitly supplied and provenance-bound).
- Combined guest-count bound, age ordering and consistency with child counts.
- Supported currency/minor-unit handling and genuine ambiguous/conflicting details.
- No semantic promotion of a model value, quote or clarification into authority.

A model can still misinterpret a grounded phrase. Review/clarification and observed
request tests remain necessary. Do not describe structural validation as a proof of
semantic truth. Any model failure/unavailable gateway must be visible; never silently
fall back to the old sentence parser while labeling the result LLM-interpreted.

## Fixture runner contract

Each positive case supplies `id`, `request`, and a valid grounded `result`. Each
negative case supplies the same plus `validation_layer` (schema/semantic). Runtime
conformance tests must reject all negatives. The model may produce an equivalent
verbatim quote rather than the exact fixture quote; these are validator fixtures,
not exact-text success expectations for live model answers.

Examples deliberately use natural language without a rigid Plan-a-dinner prefix.
The screenshot case retains the original there/their typo and children ages 6–14.
No fixture makes a venue, availability, price, contact or authority claim. Main must
report live resolved model identity and actual token usage separately from this
model-controlled result; the supervisor-approved model policy remains code-owned.
