# Talking to OpenEMR

Everything below was established against a running OpenEMR 8.6, by trying it.
None of it is in the FHIR spec, and most of it is not in OpenEMR's docs either.

## Why OpenEMR

It is the only HMS we evaluated whose **actual UI can appear in the demo**,
because it is ours on localhost rather than someone else's hosted box. It also
does what Bahmni could not: approval `Task` resources that actually write
(Bahmni 500s on every `POST /Task`), and `ServiceRequest`/`Observation` for the
lab beat.

The trade is name-recognition. Bahmni is the HMS an Indian hospital judge
knows. Say that out loud rather than hoping nobody asks.

## The thing that had to be true

A judge cancels an appointment in OpenEMR's own calendar. The calendar writes
`pc_apptstatus = 'x'` to `openemr_postcalendar_events` — it does not go near
FHIR. The question was whether our connector, which reads FHIR, would see it.

It does. Verified:

    FHIR status before:  booked
    UPDATE openemr_postcalendar_events SET pc_apptstatus='x'
    FHIR status after:   cancelled

FHIR is a live projection of the same tables the UI writes, so anything a
person does in the UI is visible to the connector on the next poll. This is the
single fact the whole demo rests on.

## One-time instance setup

The API is **off by default** and a registered client is **disabled by
default**. Both are silent: you get `API is disabled` and then
`invalid_client`, neither of which says what to turn on.

    UPDATE globals SET gl_value='1'
      WHERE gl_name IN ('rest_api','rest_fhir_api','rest_system_scopes_api');
    UPDATE globals SET gl_value='http://localhost:8300' WHERE gl_name='site_addr_oath';
    -- after registering a client:
    UPDATE oauth_clients SET is_enabled=1, client_role='system';

## Auth: SMART Backend Services

`client_credentials` requires `private_key_jwt` — a client secret is refused
with `jwks is invalid`. The flow:

1. Generate an RSA keypair.
2. `POST /oauth2/default/registration` with an inline `jwks`, a
   `redirect_uris` array (**required even for client_credentials**, otherwise
   `redirect_uris is invalid`), and every scope you will ever ask for. Scopes
   are fixed at registration: asking for `system/Patient.write` later when you
   registered only `.read` fails as a bare `401 Unauthorized`.
3. Enable the client (above).
4. Sign an `RS384` assertion (`iss`/`sub` = client_id, `aud` = the token URL)
   and exchange it.

**Tokens last 300 seconds.** A connector polling for minutes needs a provider
that re-mints, not a token fetched once at startup.

## Writing resources

Every one of these was a 400 before it was a 201.

**Patient** — `name[].use` must be `"official"`. Without it the validator
reports `fname`/`lname` empty, having not read the name at all.

**Patient identifiers are not yours.** We send `EKY-KAMLA`; OpenEMR stores its
own auto-assigned PID (`"1"`) and discards ours. `HospitalLinks.patient_for`
matches families on identifier, so the seed must record OpenEMR's id rather
than assume ours survives.

**Appointment** needs all of:
- a `Patient` participant that resolves, and
- a `Location` participant whose UUID is a row in the **`facility`** table.
  The FHIR `Location` search also returns patient home addresses, and those
  resolve to nothing. Get the real one from
  `SELECT id, name, HEX(uuid) FROM facility`.
- a `comment` — maps to `pc_hometext`, which is `NotEmpty`. `description`
  does not satisfy it.

`Appointment` supports `create` but **not `update`**. Cancelling through FHIR
is not possible; cancelling through the UI is, which is what the demo needs
anyway.

## Search parameters

`Appointment` supports exactly `patient, _id, date, _lastUpdated`. There is
**no `_include`**, so the trick our HAPI adapter uses — pulling the patient
into the same bundle to get the identifier — does not work here. Resolve
`Patient/{id}` separately and cache it.

## Local instance

    cd /tmp/hms-eval && docker compose -f openemr-min.yml up -d
    # UI:   http://localhost:8300   admin / pass
    # FHIR: http://localhost:8300/apis/default/fhir

## Driving the calendar UI

Every one of these was found by failing at it first.

**The appointment must have a provider.** `pc_aid` is NULL after a FHIR
create, and the calendar lays appointments out in per-provider columns — so a
provider-less appointment is in the database, readable over FHIR, and
**invisible on the calendar**. Include a `Practitioner` participant, or the
demo has nothing to click.

**Date navigation does not respond to URL parameters.** Neither
`?year=&month=&day=` on the calendar frame nor a direct
`interface/main/calendar/index.php?...&Date=` works; the frame reloads on
today regardless. Rather than fight it, put the appointment on *today* — the
calendar opens there, and a slot being cancelled today is a perfectly natural
demo anyway.

**The page is frames.** After login the calendar is a child frame:

    const cal = page.frames().find(f => f.url().includes("calendar/index.php"));

**Dismiss the registration modal first**, with "Ask again later" — not
"Submit", which opts the installation into telemetry.

**The appointment block is `div.event_appointment[data-eid]`, and it opens on
double click**, not click. Clicking the patient name inside it opens the
patient chart instead, which looks like the right thing happening and is not.

**A confirm dialog blocks the save.** "Provider not available, use it anyway?"
Without a handler the save silently never happens — the editor stays open, the
database is unchanged, and nothing reports an error:

    page.on("dialog", async d => { await d.accept(); });

Full flow, verified:

    log in → "Ask again later" → dblclick div.event_appointment
    → select[name=form_apptstatus] = 'x' → click #form_save → accept dialog

    FHIR status after: cancelled

## Patient identifiers: they can be ours after all

A first attempt sent `identifier: [{system: "https://ekyatra.org/id", value:
"EKY-KAMLA"}]` and OpenEMR stored its own auto-assigned PID instead, silently.
The identifier FHIR reports is OpenEMR's **External ID** (`patient_data.pubpid`),
and it is only written when the identifier carries the v2-0203 `PT` type coding:

    "identifier": [{
      "use": "official",
      "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0203",
                           "code": "PT"}]},
      "system": "http://terminology.hl7.org/CodeSystem/v2-0203",
      "value": "EKY-KAMLA"
    }]

With that shape the seeder is pure FHIR and needs no database access, and the
Bahmni contract survives unchanged: **the identifier the hospital holds is the
patient id here**, which is what `HospitalLinks.patient_for` matches on.
