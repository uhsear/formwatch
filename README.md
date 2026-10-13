# formwatch

Tell a broken Survey123 form from a quiet one. Reads the form's submission layer and returns
BROKEN, SILENT, QUIET or REFUSED. Refuses to judge a seasonal form in its first year, because
one year of data cannot show whether a silence is a season or a failure.

Here is a constructed case. It is not a report of one organization's incident.

Take a seasonal inspection form. Crews use it every day from June to the middle of August, and
almost never after that. In October it has been silent for six weeks, and nobody worries. In
February it has been silent for six months, and nobody worries. Then June comes, the season opens,
and the form is still silent. A change to the layer in the spring removed the right to add rows.
Every crew's phone shows the form, fills it in, and fails on submit. The map that counts
submissions does not move. Nothing errors, because a map that does not move is not an error.

The silence looks the same whether the form is broken or the work has not started. Staff learn
about it from a person who asks where their report went, weeks into the season.

The obvious fix is a cadence alarm: alert when no submission arrives for N days. In a form's first
year it fires on every winter. The alarm cries wolf, someone turns it off, and the form breaks the
following June with no alarm left. This tool treats the first year as a case it cannot decide. It
says so with its own exit code instead of guessing.

## What exists already

- **The Survey123 website** shows each response with its editor tracking fields. Esri documents
  that `CreationDate` records "when the data was saved into the ArcGIS feature layer" and that
  `EditDate` records edits. A person can read the newest date there. It is a page someone has to open,
  and it compares that date with nothing.
- **[Esri/Survey123-tools](https://github.com/Esri/Survey123-tools)** is a collection of notebooks
  and guides that automate Survey123 tasks with the ArcGIS API for Python. Good if you want to
  build your own check. Its folders cover cloning, exporting, updating, reports, survey data and
  publishing. I found no silence check in them.
- **[InternationalCraneFoundation/Survey123_reporter](https://github.com/InternationalCraneFoundation/Survey123_reporter)**
  checks a layer for new records and emails them. It answers "what arrived", not "why did nothing
  arrive".
- **[voidstackloop/formwatch](https://github.com/voidstackloop/formwatch)** shares the name. It is a
  Rust command line tool that drives a browser through public-service web forms to find broken
  flows and accessibility problems. It tests a form by using it. This tool never submits anything
  and judges a form from its submission history. Different tool, same name.
- Several JavaScript libraries are also named formwatch or formwatcher, such as
  [kylefox/jQuery-FormWatch](https://github.com/kylefox/jQuery-FormWatch). They watch a form in a
  browser for changed input. They do not read a feature layer.

None of these compares the current silence with the form's own history, and none refuses when
the history is too short. A search for the name and for Survey123 monitoring tools on GitHub on
2026-10-13 found no other tool that does. A search cannot prove there is none.

## How it decides

```
$ python formwatch.py --self-test
formwatch self-test: no ArcGIS, no credentials, loopback stub only
--------------------------------------------------------------------
PASS  2026-01-15 parses to its epoch ms
...
PASS  a layer without Create is BROKEN  <-- pinned defect
...
PASS  a season that ended two days earlier than last year is QUIET on 2026-01-15  <-- pinned defect
PASS  a seasonal form in its first winter is REFUSED  <-- pinned defect
PASS  the plain gap rule calls that same first winter SILENT, which is the misfire the refusal exists to stop  <-- pinned defect
...
PASS  a unique prefix --ap of --apply is REFUSED by the parser  <-- pinned defect
...
PASS  a token and a proxy in the caller's environment never reach the stub  <-- pinned defect
...
PASS  stub: a rejected token is not printed back  <-- pinned defect
...
--------------------------------------------------------------------
137 assertions, 0 failed
```

The verdicts, in the order the tool checks them:

| Verdict | Exit | Meaning |
|---|---|---|
| `BROKEN` | 1 | A fact. The layer's `capabilities` do not include `Create`, so the form cannot add a submission. Needs no history. |
| `REFUSED` | 4 | The data cannot support a verdict. See the list below. Nothing was decided. |
| `SILENT` | 1 | The silence is longer than this form's own history explains. Investigate. |
| `QUIET` | 0 | The silence is inside what the history shows. This is not proof that the form works. |

The tool refuses when:

- the layer reports no `capabilities`, no date field of the requested name, or a field that is not
  a date;
- the read stopped before the end of the layer (`--max-rows`, or a layer that cannot page);
- there is no dated submission, or fewer than 30;
- with `--seasonality none`, the history spans less than 28 days;
- the newest submission is dated after now;
- the form has no seasonal exemption and its history is shorter than one year plus the length of
  the current silence. This is the first-year rule.

Here is the first-year rule, run through the command line against the self-test's stub. The form
has one season of data, June 1 to August 15, and the date is January 15. The layer url is shortened
to `<layer>`, and the exit code is not part of the output:

```
$ python formwatch.py --layer-url <layer> --now 2026-01-15
formwatch: <layer>
--------------------------------------------------------------------
verdict      REFUSED
  first year: the history is 227 days and a seasonal form needs 518 (a year plus this silence) to show whether the silence is a season; pass --seasonality none to say it has none
submissions  76 dated, 0 undated, first 2025-06-01T12:00:00Z, last 2025-08-15T12:00:00Z
```

The exit code is 4. With `--seasonality none` the same data gives `SILENT` and exit 1, because the
plain gap rule sees 152.5 days of silence against a form that normally waits one day. That is the
false alarm. Give the tool a second season and the same January reads:

```
$ python formwatch.py --layer-url <layer> --now 2026-01-15
formwatch: <layer>
--------------------------------------------------------------------
verdict      QUIET
  a year ago on this date the form was in a 290 day silence, so this silence repeats a season
submissions  152 dated, 0 undated, first 2024-06-01T12:00:00Z, last 2025-08-15T12:00:00Z
```

The exit code is 0. In June, with the season open and no submission, the same data reads `SILENT`
(308.5 days against a limit of 3.0), because a year ago the form was submitting on that date.

The cadence rule has two parts.

1. **The gap limit.** The limit is three times the 95th percentile of the gaps between submissions,
   never under one day. A silence at or under the limit is `QUIET`.
2. **The year-ago silence.** Unless you pass `--seasonality none`, a silence over the limit is
   compared with the date one year earlier. The tool finds the gap between submissions that
   contained that date. If that gap lasted at least 0.75 as long as the current silence, the
   silence repeats a season and the verdict is `QUIET`. If not, the verdict is `SILENT`. Seasons
   never end on the same day twice, so the match is 0.75 and not 1.0. If no submission followed that
   date, the silence is over a year long and the verdict is `SILENT`.

## Requirements

Python 3.9 or newer. The standard library only. No `arcpy`, no `requests`, nothing to install.

```
git clone https://github.com/uhsear/formwatch.git
```

## Quick start

```
python formwatch.py --self-test
python formwatch.py --layer-url https://host/arcgis/rest/services/survey/FeatureServer/0
```

The url is the submission layer, ending `/FeatureServer/<n>`. A url that carries a token, a user
name, a query string or a fragment is refused before any request, because urllib writes the url
back into its own error text.

## Usage

Check first. Nothing is written without `--apply`.

```
python formwatch.py --layer-url URL
python formwatch.py --layer-url URL --seasonality none
python formwatch.py --layer-url URL --date-field CreationDate --now 2026-06-20
python formwatch.py --layer-url URL --portal https://org.example.com --username builder
python formwatch.py --layer-url URL --out verdict.json --apply
```

| Flag | Default | What it does |
|---|---|---|
| `--layer-url` | none | The survey's submission layer. Required. |
| `--portal` | none | Portal that issues the token. Needed with `--username`. Must be https. |
| `--username` | none | Generate a token for this user. |
| `--date-field` | `CreationDate` | Submission time field. Matched without regard to case. |
| `--seasonality` | `unknown` | `unknown` refuses a first year. `none` states that the form has no seasonal cycle. |
| `--now` | the clock | Treat this UTC time as now. `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SSZ`. |
| `--max-rows` | `50000` | About this many rows to read. The last page may add more. A history cut short is refused. |
| `--out` | none | New JSON file for the verdict. |
| `--apply` | off | Create the `--out` file. Without it nothing is written. |
| `--self-test` | off | Run the offline assertions and exit. |

The report written by `--out --apply` is JSON with sorted keys. It holds the verdict, the reasons,
the counts, the silence and limit in days, and `year_ago_gap_days`, which is null when the year-ago
comparison did not run.

Credentials never go on the command line. Put a token in `FORMWATCH_TOKEN`. For `--username`, put
the password in `FORMWATCH_PASSWORD` or answer the unechoed prompt. Setting both
`FORMWATCH_TOKEN` and `--username` is a usage error. Public layers need neither. The tool sends
the token in the request body, never in the url.

Exit codes: 0 QUIET, 1 BROKEN or SILENT, 2 the layer could not be read or the report could not be
written, 4 REFUSED, 64 usage error, 70 an unexpected crash. A crash is never a finding.

## Configuration

The limits are constants at the top of `formwatch.py`, not flags. Change them in the file.

| Constant | Value | Why |
|---|---|---|
| `MIN_SUBMISSIONS` | 30 | The 95th percentile of fewer than 20 gaps is just the largest gap. |
| `GAP_MULTIPLIER` | 3.0 | A judgement, not a measurement. Tune it on your own form's history. |
| `MIN_LIMIT_DAYS` | 1.0 | A form with bursts of submissions is not silent after an hour. |
| `MIN_SPAN_DAYS` | 28 | With `--seasonality none`, thirty submissions in one afternoon show no cadence. |
| `YEAR_DAYS` | 365 | A leap year shifts the comparison by one day. |
| `SEASON_MATCH` | 0.75 | How long last year's silence must have lasted, as a share of this one. A judgement. |

## Limits

What the tool does not know, and the ways it can be wrong.

**What it cannot see**

- It does not know why a form is silent. `SILENT` means investigate. A Survey123 author can close a
  survey, or schedule it to open and close, on the Collaborate page of the web app (Esri:
  "You can either open and close a survey manually or schedule it to only be open for a specific
  time."). I could not confirm from the documentation whether closing a survey changes the layer's
  `Create` capability. If it does, a survey closed on purpose reads `BROKEN`. If it does not, it
  reads `SILENT` once the closure outlasts the history.
- It cannot see a form that is broken for a reason other than a missing `Create`: a field-app
  version that fails on submit, a bad choice list, a revoked sharing level. The layer looks fine and
  the people cannot submit. Only the silence shows it, and `SILENT` is the only signal.
- `CreationDate` is the time the row reached the layer, not the time the person filled in the form.
  Esri's documentation says so. Phones that sync a week of work at once look like one burst.
- Editor tracking must be on. If the date field is missing the tool refuses.
- Related tables (repeat questions) are not read. Point `--layer-url` at the main layer.
- A layer that is copied or republished may reset `CreationDate` to the copy date. Then the history
  before the copy is invisible to the first-year rule. I did not check this against Esri's
  documentation.

**When the verdict can be wrong**

- The year-ago test returns `QUIET` whenever the form sat in a silence on this date a year ago that
  lasted at least 0.75 as long as this one. The gap limit does not matter then. A daily form with a
  yearly holiday closure that breaks at the start of the closure reads `QUIET` until the date a year
  ago falls after the closure.
- A form whose season opens a few days later than last year reads `SILENT` early in the season.
  `SEASON_MATCH` is a judgement, not a measurement.
- The first submission is taken as the start of the form's history, so delete test records or a bad
  early date before you trust a `QUIET`. One stray old value, such as 0 or 1999, makes the form look
  decades old and hides a first year. This needs a date field that people or a migration fill in.
  The default `CreationDate` is set by the system.
- A stray row in an off season shortens the silence that the next year compares with. It can turn
  the last weeks of the off season `SILENT`.
- Duplicate object ids across pages are not detected. Duplicate stamps count twice and shrink the
  gap percentile a little.
- The tool uses the local clock and the server's date values. A clock that is wrong by hours does
  not change a verdict that is measured in days. A clock wrong by days does.

**Reading the service**

- A layer that cannot sort (`supportsOrderBy` false) or page (`supportsPagination` false) is read
  as the server gives it. A new submission during the read can shift the pages. A layer that cannot
  page and has more rows than one response holds is refused.
- The tool asks for an ordered read when `advancedQueryCapabilities.supportsOrderBy` is true. The
  Esri Query page ties `orderByFields` to `supportsAdvancedQueries`. A layer that reports one flag
  true and the other false may reject the query, and the tool then exits 2. I did not run this
  against a real service.
- A busy form over 50000 rows needs `--max-rows` raised to be judged at all. The cap stops paging,
  not the size of a page, so about one page more can be read. A layer that fits in one response is
  read whole whatever `--max-rows` says.
- A service reply that is valid JSON but has an unexpected shape ends in exit 70 or 2, never in a
  verdict. Examples are a query reply that is a list, features that are not objects, a field with a
  null name, and a date outside the years 1 to 9999. A `capabilities` value that is a list reads as
  lacking `Create`.
- A closed stdout pipe, such as `| head`, ends the run without a meaningful exit code. It exited 120
  on Windows in a test. If `--date-field` names a field that is not ASCII and stdout cannot encode
  it, the run exits 70. Use a UTF-8 stdout in that case.
- The reply is read whole with no size cap, and the timeout is per request, not per run. A server
  that streams a very large or very slow reply can hold the tool open or use a lot of memory.
- There is no `--insecure` flag. A portal with a private certificate authority needs its
  certificate trusted by the machine.
- The `--username` path has only run against the local stub. The tool calls `generateToken` with
  `client` set to `referer`, but later requests send no `Referer` header. A real service may refuse
  the token, and the tool then exits 2. It never gives a verdict in that case.
- It has never been run against a real ArcGIS Online or Enterprise service. See the next section.

**Credentials and writes**

- The tool sends `FORMWATCH_TOKEN` over whatever scheme `--layer-url` uses. Use an https layer url
  whenever a token is set. Only `--portal` is forced to https.
- Requests go through any proxy that the environment configures, and an http proxy sees the token.
- The tool follows HTTP 301, 302 and 303 redirects. Python re-sends the request as a GET with no
  body, so the token and password are not forwarded. The service then sees no token, and the run
  usually exits 2. A hostile server can still make the tool request another address.
- Redaction matches exact text. A credential echoed URL-encoded or in another case is not redacted.
  The crash traceback redacts only credentials taken from the environment.
- Use a new `--out` file name for each run. A write failure exits 2 after the verdict is printed and
  replaces the verdict's exit code, so a cron job with a fixed path reports 2 on every run after the
  first.
- `--apply` is the only way the command line writes. `--self-test` still creates a temporary folder
  in the system temp directory and removes it when it ends.

**The self-test**

- The self-test removes `FORMWATCH_TOKEN`, `FORMWATCH_PASSWORD` and every `*_proxy` variable from its
  own environment before it starts the stub, and restores them when it ends.
- A crash inside `--self-test` exits 1, the same code as `SILENT`. Read the footer line, not the exit
  code, to tell a crash from a finding.
- A few input and output details have no pinning assertion. The real clock is checked only to within
  ten years. The `--max-rows` stop is not tested at its exact boundary. No assertion runs the file as
  a subprocess, so a `main()` that returned nothing under `--self-test` would exit 0 without running
  a test.

## What was exercised, and where

Every network path was run against a local `http.server` stub that answers the way the cited
documentation says a service does. The stub is not ArcGIS. It models the documented fields and
the documented failure shapes. A real service may differ, and a first run against one is still
owed.

| Claim | Where it was exercised |
|---|---|
| Quiet, silent, broken and refused each reach the right exit code | Self-test, boundary cases and the stub; also four runs of the command line against the stub, the output quoted above |
| A seasonal form's first winter is refused, not called broken | Self-test, `pinned defect`; also a command line run against the stub |
| `--seasonality none` states "no season" and reaches `SILENT` | Self-test and a command line run |
| A season that ends a few days earlier than last year, or a stray off-season row, still reads `QUIET` | Self-test, `pinned defect`, with the boundary at 0.75 pinned to the millisecond |
| The limit is strictly greater than, and has a one day floor | Self-test, millisecond boundary and an hourly form |
| A missing `Create` is `BROKEN`, and no row is read | Stub: the query log is empty |
| Rows are read in object id order, paged by rows returned, not rows asked | Stub that returns 7 rows per page: 22 pages, offsets 0, 7, 14 |
| A layer that cannot page, or a read cut by `--max-rows`, is refused | Stub, four cases: unpaged and truncated, cut by `--max-rows`, endless empty page, repeated full page |
| A service error inside an HTTP 200 body is a failure | Stub, `error200` and `errorstr` faults |
| An HTTP 500, a non-JSON body and a body that is not a layer are failures | Stub, three faults |
| A token or password reaches no stream and no file | Stub that echoes the token in its own error; transport error that quotes the url; report file read back |
| The self-test sends no credential of the caller's to the stub, even with a proxy set | Self-test, `pinned defect`: a planted token and proxy; also a run with `HTTP_PROXY` pointed at a counting listener saw 0 requests |
| The token travels in the request body | Stub log: every request carries it in the body and no path has a query string |
| `generateToken` is called with `client` and `referer`, and a password is refused over http to a remote host | Stub and usage tests |
| Only `--apply` writes. An existing report is never overwritten | Self-test: file absent after the check, one file after `--apply`, exit 2 on the second |
| `--ap`, `--layer` and `--self` are refused by the parser, with exit 64 | Self-test, `pinned defect`; also a real run |
| A crash exits 70 with the token redacted | Self-test, a crash that carries the token |
| A form with no listening service exits 2 | Self-test |

Coverage and hosts, measured on 2026-10-13 with `coverage run --branch formwatch.py --self-test`:

```
Name           Stmts   Miss Branch BrPart  Cover
formwatch.py     711      0    160      0   100%
```

| Host | Python | Assertions |
|---|---|---|
| Windows | 3.13 | 137 |
| Windows | 3.12 | 137 |
| Windows | 3.9 | 137 |
| Linux | 3.12 | 137 |

The counts are equal because no assertion depends on the platform. The Linux host has no
`coverage` package, so coverage was measured on Windows only. `pyflakes` is clean and
`python -W error` compiles and runs the self-test clean. A hand-run sweep of single-line mutations
of the decision core and the credential handling is how the boundary assertions were found. An
earlier sweep left three mutations of `judge` alive, at the percentile sort and at the first-year and
28 day boundaries. Each now has an assertion, and a final sweep of 14 mutations killed all of
them. The sweep script is not part of the repo, so a reader cannot rerun it.

## Sources

Behaviour the tool relies on, and where the documentation says so.

- [Query (Feature Service/Layer)](https://developers.arcgis.com/rest/services-reference/enterprise/query-feature-service-layer/):
  the operation accepts GET and POST. Date values are returned as Unix epoch milliseconds, in UTC.
  `resultOffset` applies only if `supportsPagination` is true. The page ties `orderByFields` to
  `supportsAdvancedQueries`, but the tool gates the ordered read on `supportsOrderBy` in
  `advancedQueryCapabilities`. See Limits. "There are no more pages to fetch when
  `exceededTransferLimit: false` is present in the response."
- [Feature layer](https://developers.arcgis.com/rest/services-reference/enterprise/layer-feature-service/):
  `capabilities` returns Query, Create, Delete, Update, Editing, Sync, Uploads and Extract, as a
  comma separated string. `advancedQueryCapabilities` holds `supportsPagination` and
  `supportsOrderBy`. A field of type `esriFieldTypeOID` is the object id, and
  `esriFieldTypeDate` is a date.
- [Generate Token](https://developers.arcgis.com/rest/users-groups-and-items/generate-token/):
  POST, "only allowed over HTTPS", with `username`, `password`, `client`, `referer` and
  `expiration`. The reply holds `token` and `expires`. The tool allows plain http to a loopback
  address only, so the stub can run.
- [Scripting with the ArcGIS REST API](https://enterprise.arcgis.com/en/portal/11.4/administer/linux/scripting-with-the-arcgis-rest-api.htm):
  the sample checks `'error' in jsonResponse` after a call. The tool reads the body for an
  `error` object on every call and treats it as a failure whatever the HTTP status.
- [Tracking surveys](http://doc.arcgis.com/en/survey123/analyze/trackingsurveys.htm):
  with editor tracking on, `CreationDate` records when the data was saved into the feature layer,
  and "will not tell you when the data was captured".
- [Share surveys](http://doc.arcgis.com/en/survey123/analyze/sharesurvey.htm): a survey can be
  opened and closed manually or scheduled, and "Closing a survey to responses is not honored by
  the Survey123 field app."

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [sharewatch](https://github.com/uhsear/sharewatch) - diffs an organization's sharing posture, so a form made private is not mistaken for a quiet one
- [restfake](https://github.com/uhsear/restfake) - a mock ArcGIS REST server that fails the way ArcGIS fails. formwatch's self-test does not use it. I ran formwatch against it by hand with 300 rows. Its default layer has no `CreationDate`, so the run was `REFUSED`, and `--date-field LASTEDIT` read all 300 rows. restfake reports `supportsPagination` at the top of the layer JSON, not under `advancedQueryCapabilities`, so formwatch reads its layers as unpaged and refuses one with more rows than a page
- [feedstamp](https://github.com/uhsear/feedstamp) - the same refusal to act on a delivery that is not new and complete
