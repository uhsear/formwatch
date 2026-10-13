#!/usr/bin/env python3
"""Tell a broken Survey123 form from a quiet one, and refuse to guess in a seasonal form's first year.

A Survey123 form that stopped working and a form nobody has needed this month look
identical from outside: the layer behind it gains no rows. Nothing errors. The map that
counts submissions simply stops moving, and the first alarm is a person asking where
their report went.

This reads the form's submission layer (a hosted feature layer) and gives one of four
verdicts:

    BROKEN   a fact, not a guess: the layer no longer allows Create, so the form cannot
             add a submission.
    SILENT   the current silence is longer than this form's own history explains.
    QUIET    the silence is within what this form's own history shows. It is not proof
             the form works.
    REFUSED  the history cannot support a verdict. The most important case: a form with
             less than a year of history plus the length of the current silence. A
             seasonal form looks dead every off season, and one year of data is too
             little to say whether the silence is a season or a failure.

The cadence test compares the silence with three times the 95th percentile of the gaps
between submissions. Unless you state --seasonality none, a silence over that limit is
also compared with the silence the form was in one year ago, on the same date. It is
QUIET when the form was in a silence then that lasted at least three quarters as long. It reads only. The one thing it can write is a small JSON report, and that needs
--apply.

    python formwatch.py --self-test
    python formwatch.py --layer-url https://host/arcgis/rest/services/survey/FeatureServer/0
    python formwatch.py --layer-url URL --seasonality none
    python formwatch.py --layer-url URL --date-field CreationDate --now 2026-06-20
    python formwatch.py --layer-url URL --portal https://org.example.com --username builder
    python formwatch.py --layer-url URL --out verdict.json --apply

Exit codes: 0 QUIET, 1 BROKEN or SILENT, 2 the layer could not be read or the report
could not be written, 4 REFUSED (nothing was decided), 64 usage error, 70 an unexpected
crash, which is never a finding.
"""

from __future__ import print_function

import argparse
import bisect
import contextlib
import datetime
import getpass
import http.server
import io
import json
import math
import os
import re
import runpy
import shutil
import sys
import tempfile
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

DAY_MS = 86400000

# One year, in days, for the same-stretch-last-year comparison. A leap year shifts
# the comparison by one day, which is far inside the length of any silence worth
# judging.
YEAR_DAYS = 365

# Fewest dated submissions a verdict needs. The 95th percentile of fewer than 20 gaps
# is simply the largest gap, which is no statistic at all. 30 submissions give 29 gaps
# and leave some margin.
MIN_SUBMISSIONS = 30

# With --seasonality none, the history must span at least this many days. Thirty
# submissions in one afternoon fit the count rule and still say nothing about cadence.
MIN_SPAN_DAYS = 28

# Silence is SILENT when it exceeds this multiple of the 95th percentile gap. Three is
# a judgement, not a measurement. Tune it against your own form's history.
GAP_MULTIPLIER = 3.0
GAP_QUANTILE = 0.95

# A silence over the limit is QUIET when, one year ago on the same date, the form was
# inside a silence at least this fraction as long. Seasons never end on the same day
# two years running, so demanding an equal length would call a season that ended two
# days early SILENT all winter. Three quarters is a judgement, like the multiplier.
SEASON_MATCH = 0.75

# The limit is never below this many days, so a form with sub-minute bursts is not
# SILENT after an hour of quiet.
MIN_LIMIT_DAYS = 1.0

# Most rows read from the layer. A history cut short by this cap is REFUSED, because
# the true first submission is then unknown and the year-ago test would lie.
MAX_ROWS = 50000

# Rows asked for per page. The layer's maxRecordCount lowers it when it is smaller.
PAGE_SIZE = 1000

HTTP_TIMEOUT = 60
REDACTED = "[redacted]"
DEFAULT_DATE_FIELD = "CreationDate"
TOKEN_ENV = "FORMWATCH_TOKEN"
PASSWORD_ENV = "FORMWATCH_PASSWORD"

EXIT = {"QUIET": 0, "BROKEN": 1, "SILENT": 1, "REFUSED": 4}


# ------------------------------------------------------------------ pure core

def parse_when(text):
    """Parse YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ (UTC) into epoch milliseconds."""
    for pattern in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            stamp = datetime.datetime.strptime(text, pattern)
        except ValueError:
            continue
        delta = stamp - datetime.datetime(1970, 1, 1)
        return (delta.days * 86400 + delta.seconds) * 1000
    raise ValueError("%r is not YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ" % (text,))


def iso(ms):
    """Epoch milliseconds to a UTC ISO string. Arithmetic, not utcfromtimestamp,
    which Python 3.12 deprecates."""
    when = datetime.datetime(1970, 1, 1) + datetime.timedelta(milliseconds=ms)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def utcnow_ms():
    delta = (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
             - datetime.datetime(1970, 1, 1))
    return (delta.days * 86400 + delta.seconds) * 1000


def quantile(values, q):
    """Nearest-rank quantile of an ascending, non-empty list."""
    index = int(math.ceil(q * len(values))) - 1
    return values[max(index, 0)]


def redact(text, *secrets):
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), REDACTED)
    return text


def plain_url(url, need_layer):
    """True when url is http(s) with no credentials, query string or fragment.

    A token pasted into the url would be written back out by urllib in its own error
    text, so a url that carries one is refused before any request is made.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    if "@" in parts.netloc or parts.query or parts.fragment:
        return False
    if need_layer and not re.search(r"/FeatureServer/\d+/?$", parts.path):
        return False
    return True


def is_loopback(url):
    return urllib.parse.urlsplit(url).hostname in ("localhost", "127.0.0.1", "::1")


def check_layer(layer):
    """Read what the layer's own metadata proves.

    Returns (problems, refusal, fields). problems are facts that make the form unable
    to take a submission, and each one is BROKEN. refusal is a reason the metadata
    cannot be used to read a history, and it is REFUSED. fields maps lower case name
    to (name, type).
    """
    problems = []
    refusal = None
    fields = dict((f.get("name", "").lower(), (f.get("name"), f.get("type")))
                  for f in layer.get("fields") or [])
    if "capabilities" not in layer:
        refusal = "the layer reports no capabilities, so Create cannot be checked"
    else:
        caps = [c.strip() for c in str(layer["capabilities"]).split(",")]
        if "Create" not in caps:
            problems.append("layer capabilities are %r and lack Create: the form "
                            "cannot add a submission" % ",".join(caps))
    return problems, refusal, fields


def pick_fields(layer, fields, wanted):
    """Return (date field, oid field, paged, ordered, refusal)."""
    got = fields.get(wanted.lower())
    if got is None:
        return None, None, False, False, "the layer has no field named %s" % wanted
    if got[1] != "esriFieldTypeDate":
        return None, None, False, False, ("%s is %s, not esriFieldTypeDate"
                                          % (got[0], got[1]))
    oid = None
    for name, kind in fields.values():
        if kind == "esriFieldTypeOID":
            oid = name
    adv = layer.get("advancedQueryCapabilities") or {}
    paged = adv.get("supportsPagination") is True
    ordered = adv.get("supportsOrderBy") is True and oid is not None
    return got[0], oid, paged, ordered, None


def judge(problems, refusal, stamps, undated, complete, now_ms, seasonality):
    """The decision. Pure: no clock, no network, no file.

    BROKEN wins over everything. Then every REFUSED reason. Then the cadence test.
    """
    out = {"verdict": None, "reasons": [], "submissions": len(stamps),
           "undated": undated, "first": None, "last": None, "silence_days": None,
           "limit_days": None, "year_ago_gap_days": None, "seasonality": seasonality}

    def done(verdict, *reasons):
        out["verdict"] = verdict
        out["reasons"] = list(reasons)
        return out

    if problems:
        return done("BROKEN", *problems)
    if refusal:
        return done("REFUSED", refusal)
    if not complete:
        return done("REFUSED", "the history is incomplete: the layer was not read "
                    "to its end, so the first submission is unknown")
    stamps = sorted(stamps)
    if not stamps:
        return done("REFUSED", "no dated submission exists, and a form that never "
                    "worked reads the same as one never used")
    first, last = stamps[0], stamps[-1]
    out["first"], out["last"] = iso(first), iso(last)
    if len(stamps) < MIN_SUBMISSIONS:
        return done("REFUSED", "%d dated submissions is under the %d a cadence needs"
                    % (len(stamps), MIN_SUBMISSIONS))
    silence = now_ms - last
    if silence < 0:
        return done("REFUSED", "the newest submission is dated after now, so the "
                    "clock or the data is wrong")
    out["silence_days"] = round(silence / float(DAY_MS), 2)
    window = max(silence, DAY_MS)
    year = YEAR_DAYS * DAY_MS
    if seasonality == "none":
        if last - first < MIN_SPAN_DAYS * DAY_MS:
            return done("REFUSED", "the history spans %d days, under the %d that "
                        "--seasonality none needs"
                        % ((last - first) // DAY_MS, MIN_SPAN_DAYS))
    elif now_ms - first < year + window:
        return done("REFUSED", "first year: the history is %d days and a seasonal "
                    "form needs %d (a year plus this silence) to show whether the "
                    "silence is a season; pass --seasonality none to say it has "
                    "none" % ((now_ms - first) // DAY_MS,
                              int(math.ceil((year + window) / float(DAY_MS)))))
    gaps = sorted((b - a) / float(DAY_MS) for a, b in zip(stamps, stamps[1:]))
    limit = max(MIN_LIMIT_DAYS, GAP_MULTIPLIER * quantile(gaps, GAP_QUANTILE))
    out["limit_days"] = round(limit, 2)
    odd = silence / float(DAY_MS) > limit
    if odd and seasonality != "none":
        # The first-year rule above guarantees a submission before the date a year
        # ago, so stamps[idx - 1] exists. None after it means the silence is longer
        # than a year, and no silence a year ago can explain that.
        idx = bisect.bisect_right(stamps, now_ms - year)
        if idx < len(stamps):
            ago = stamps[idx] - stamps[idx - 1]
            out["year_ago_gap_days"] = round(ago / float(DAY_MS), 2)
            if ago >= SEASON_MATCH * silence:
                return done("QUIET", "a year ago on this date the form was in a %d "
                            "day silence, so this silence repeats a season"
                            % int(ago // DAY_MS))
    if odd:
        return done("SILENT", "silent %.1f days against a limit of %.1f days (%g "
                    "times the 95th percentile gap)"
                    % (silence / float(DAY_MS), limit, GAP_MULTIPLIER))
    return done("QUIET", "silent %.1f days, inside the limit of %.1f days"
                % (silence / float(DAY_MS), limit))


def render(result, layer_url):
    lines = ["formwatch: %s" % layer_url, "-" * 68]
    lines.append("verdict      %s" % result["verdict"])
    for reason in result["reasons"]:
        lines.append("  %s" % reason)
    if result["first"]:
        lines.append("submissions  %d dated, %d undated, first %s, last %s"
                     % (result["submissions"], result["undated"], result["first"],
                        result["last"]))
    return lines


# ------------------------------------------------------------------- service i/o

def _call(url, params, token=None, secret=None):
    """One POST returning parsed JSON, with the service's own errors raised.

    The credential travels in the body, never in the url. The service answers some
    failures with HTTP 200 and an error object, so the body is read every time.
    """
    params = dict(params)
    params["f"] = "json"
    if token:
        params["token"] = token
    data = urllib.parse.urlencode(params).encode("utf-8")
    try:
        with contextlib.closing(urllib.request.urlopen(
                urllib.request.Request(url, data), timeout=HTTP_TIMEOUT)) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(redact("%s: %s" % (url, exc), token, secret))
    if isinstance(body, dict) and "error" in body:
        err = body["error"] if isinstance(body["error"], dict) else {}
        raise RuntimeError(redact("%s: %s %s" % (url, err.get("code"),
                                                 err.get("message")), token, secret))
    return body


def read_secret():
    secret = os.environ.get(PASSWORD_ENV)
    if secret:
        return secret
    return getpass.getpass("password (not echoed): ")


def generate_token(portal, username, secret):
    body = _call(portal.rstrip("/") + "/sharing/rest/generateToken",
                 {"username": username, "password": secret, "client": "referer",
                  "referer": portal, "expiration": 30}, secret=secret)
    if not body.get("token"):
        raise RuntimeError("generateToken returned no token")
    return body["token"]


def read_layer(layer_url, token):
    body = _call(layer_url.rstrip("/"), {}, token)
    if not isinstance(body, dict) or not isinstance(body.get("fields"), list):
        raise RuntimeError("%s did not answer with a layer description" % layer_url)
    return body


def fetch_stamps(layer_url, token, field, oid, paged, ordered, page_size, max_rows):
    """Read every value of the date field. Returns (stamps, undated, complete).

    Rows are ordered by object id ascending when the layer allows it, so a new
    submission lands after the last page rather than shifting the pages already read.
    The offset advances by the rows actually returned, because a layer may return
    fewer than asked.
    """
    stamps, undated, offset = [], 0, 0
    while True:
        params = {"where": "1=1", "outFields": field, "returnGeometry": "false"}
        if ordered:
            params["orderByFields"] = oid
        if paged:
            params["resultOffset"] = offset
            params["resultRecordCount"] = page_size
        body = _call(layer_url.rstrip("/") + "/query", params, token)
        feats = body.get("features")
        if not isinstance(feats, list):
            raise RuntimeError("the query answered without a features list")
        for feat in feats:
            value = (feat.get("attributes") or {}).get(field)
            if value is None:
                undated += 1
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                stamps.append(int(value))
            else:
                raise RuntimeError("a row holds %r in %s, which is not a date"
                                   % (value, field))
        offset += len(feats)
        if not body.get("exceededTransferLimit"):
            return stamps, undated, True
        if not feats or not paged or offset >= max_rows:
            return stamps, undated, False


def write_report(path, doc):
    """Create the report. Mode x refuses to overwrite a file that exists."""
    with io.open(path, "x", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1, sort_keys=True)
        handle.write(u"\n")


# ------------------------------------------------------------------------ cli

class _Parser(argparse.ArgumentParser):
    """Exit 64 on a usage error. argparse's own 2 would read as a failed read."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(64, "%s: error: %s\n" % (self.prog, message))


def _parse(argv):
    ap = _Parser(
        prog="formwatch.py",
        allow_abbrev=False,
        description="Tell a broken Survey123 form from a quiet one. Refuses to "
                    "judge a seasonal form in its first year.",
        epilog="Read-only. The token comes from %s and the password from %s or an "
               "unechoed prompt, never from argv." % (TOKEN_ENV, PASSWORD_ENV))
    ap.add_argument("--layer-url",
                    help="the survey's submission layer, ending /FeatureServer/<n>, "
                         "with no token in it")
    ap.add_argument("--portal",
                    help="portal url that issues the token, for --username")
    ap.add_argument("--username",
                    help="generate a token for this user (needs --portal)")
    ap.add_argument("--date-field", dest="date_field", default=DEFAULT_DATE_FIELD,
                    help="submission time field (default %s)" % DEFAULT_DATE_FIELD)
    ap.add_argument("--seasonality", choices=["unknown", "none"], default="unknown",
                    help="unknown (default) refuses a first year; none states that "
                         "the form has no seasonal cycle")
    ap.add_argument("--now", help="treat this UTC time as now (YYYY-MM-DD or "
                                  "YYYY-MM-DDTHH:MM:SSZ)")
    ap.add_argument("--max-rows", dest="max_rows", type=int, default=MAX_ROWS,
                    help="about this many rows to read; the last page may add more "
                         "(default %d)" % MAX_ROWS)
    ap.add_argument("--out", help="write the verdict as JSON to this new file")
    ap.add_argument("--apply", action="store_true",
                    help="write the --out file. Without it nothing is written.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def _usage_error(args):
    """Return the first usage problem as text, or None."""
    if not args.layer_url:
        return "--layer-url is required. Use --self-test to verify without a service."
    if not plain_url(args.layer_url, True):
        return ("--layer-url must be http(s)://host/.../FeatureServer/<n> with no "
                "credentials, query string or fragment. Put a token in %s." % TOKEN_ENV)
    if args.max_rows < 1:
        return "--max-rows must be at least 1."
    if args.apply and not args.out:
        return "--apply needs --out, the report file to create."
    if args.now:
        try:
            parse_when(args.now)
        except ValueError as exc:
            return "--now: %s" % exc
    if args.username:
        if os.environ.get(TOKEN_ENV):
            return "use %s or --username, not both." % TOKEN_ENV
        if not args.portal or not plain_url(args.portal, False):
            return "--username needs --portal, a plain http(s) url."
        if not args.portal.startswith("https://") and not is_loopback(args.portal):
            return ("--portal must be https, because generateToken carries the "
                    "password and Esri allows it only over HTTPS.")
    return None


def _run(args):
    token = os.environ.get(TOKEN_ENV) or None
    secret = None
    if args.username:
        secret = read_secret()
        token = generate_token(args.portal, args.username, secret)
    now = parse_when(args.now) if args.now else utcnow_ms()
    layer = read_layer(args.layer_url, token)
    problems, refusal, fields = check_layer(layer)
    stamps, undated, complete = [], 0, True
    if not problems and not refusal:
        field, oid, paged, ordered, refusal = pick_fields(
            layer, fields, args.date_field)
        if not refusal:
            size = layer.get("maxRecordCount")
            size = min(PAGE_SIZE, size) if isinstance(size, int) and size > 0                 else PAGE_SIZE
            stamps, undated, complete = fetch_stamps(
                args.layer_url, token, field, oid, paged, ordered, size,
                args.max_rows)
    result = judge(problems, refusal, stamps, undated, complete, now,
                   args.seasonality)
    result["now"] = iso(now)
    result["layer_url"] = args.layer_url
    for line in render(result, args.layer_url):
        print(line)
    if args.out and not args.apply:
        print("\nCheck only. No report was written. Re-run with --apply.")
    if args.out and args.apply:
        write_report(args.out, result)
        print("\nwrote %s" % args.out)
    return EXIT[result["verdict"]]


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)
    if args.self_test:
        return self_test()
    problem = _usage_error(args)
    if problem:
        print("error: %s" % problem, file=sys.stderr)
        return 64
    try:
        return _run(args)
    except (RuntimeError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except Exception:
        print("unexpected crash, not a finding:\n%s"
              % redact(traceback.format_exc(), os.environ.get(TOKEN_ENV),
                       os.environ.get(PASSWORD_ENV)), file=sys.stderr)
        return 70


# ------------------------------------------------------------------ self-test

class _Harness(object):
    """check() and raises() with a pass count and a list of failures."""

    def __init__(self):
        self.passed = 0
        self.failed = []

    def check(self, cond, label):
        if cond:
            self.passed += 1
            print("PASS  %s" % label)
        else:
            self.failed.append(label)
            print("FAIL  %s" % label)

    def raises(self, fn, label, kind=ValueError):
        try:
            fn()
        except kind:
            self.check(True, label)
        except Exception as exc:
            self.check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            self.check(False, "%s (no error raised)" % label)

    def refuses(self, argv, label):
        """argparse writes usage to stderr. Swallow it so a pass prints one line."""
        noise, sys.stderr = sys.stderr, io.StringIO()
        try:
            _parse(argv)
        except SystemExit:
            self.check(True, label)
        else:
            self.check(False, "%s (argparse accepted it)" % label)
        finally:
            sys.stderr = noise


def _footer(h):
    """Return (exit code, lines) for a finished harness."""
    total = h.passed + len(h.failed)
    if not h.failed:
        return 0, ["%d assertions, 0 failed" % total]
    return 1, (["%d assertions, %d failed" % (total, len(h.failed))]
               + ["  FAILED: %s" % label for label in h.failed])


@contextlib.contextmanager
def _hermetic():
    """Run with none of the caller's credentials or proxies in the environment.

    The README tells a user to keep a token in FORMWATCH_TOKEN, so the self-test must
    not read it. A proxy variable would also route the loopback stub through the
    proxy, and send it any credential the stub receives. NO_PROXY=* keeps the Windows
    registry proxy out too, because urllib consults the registry only when no
    environment proxy setting exists.
    """
    saved = dict(os.environ)
    for name in list(os.environ):
        if name in (TOKEN_ENV, PASSWORD_ENV) or name.lower().endswith("_proxy"):
            del os.environ[name]
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "*"
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _run_main(argv, env=None):
    """Run main() capturing both streams. Returns (code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    saved = dict(os.environ)
    os.environ.update(env or {})
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return code, out.getvalue(), err.getvalue()


def _start_stub(state):
    """A local ArcGIS-shaped service on 127.0.0.1. state drives its faults."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, body):
            raw = body.encode("utf-8") if not isinstance(body, bytes) else body
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            size = int(self.headers.get("Content-Length") or 0)
            form = urllib.parse.parse_qs(self.rfile.read(size).decode("utf-8"))
            flat = dict((k, v[0]) for k, v in form.items())
            state["log"].append((self.path, flat))
            mode = state.get("mode")
            if self.path.endswith("/generateToken"):
                if (flat.get("username"), flat.get("password")) == state["login"]:
                    return self._send(200, json.dumps(
                        {"token": state["token"], "expires": 1}))
                return self._send(200, json.dumps({"error": {
                    "code": 400, "message": "Unable to generate token.",
                    "details": []}}))
            if state.get("token") and flat.get("token") != state["token"]:
                return self._send(200, json.dumps({"error": {
                    "code": 498, "message": "Invalid token.", "details": []}}))
            if mode == "echo":
                return self._send(200, json.dumps({"error": {
                    "code": 400, "message": "bad token %s" % flat.get("token")}}))
            if mode == "http500":
                return self._send(500, "boom")
            if mode == "garbage":
                return self._send(200, "<html>not json</html>")
            if mode == "error200":
                return self._send(200, json.dumps({"error": {
                    "code": 400, "message": "Unable to complete operation."}}))
            if mode == "errorstr":
                return self._send(200, json.dumps({"error": "plain text"}))
            if mode == "notlayer":
                return self._send(200, json.dumps({"hello": 1}))
            if self.path.endswith("/query"):
                return self._query(flat)
            return self._send(200, json.dumps(state["layer"]))

        def _query(self, flat):
            mode = state.get("mode")
            rows = state["rows"]
            if mode == "nofeatures":
                return self._send(200, json.dumps({"hello": 1}))
            if mode == "endless":
                return self._send(200, json.dumps({
                    "features": [], "exceededTransferLimit": True}))
            if mode == "loop":
                return self._send(200, json.dumps({
                    "features": [{"attributes": rows[0]}] * 3,
                    "exceededTransferLimit": True}))
            offset = int(flat.get("resultOffset", 0))
            asked = int(flat.get("resultRecordCount", len(rows)))
            take = min(asked, state.get("page_cap", asked))
            page = rows[offset:offset + take]
            more = offset + len(page) < len(rows)
            return self._send(200, json.dumps({
                "features": [{"attributes": dict(r)} for r in page],
                "exceededTransferLimit": more}))

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    return server, "http://127.0.0.1:%d" % server.server_address[1]


def self_test():
    """Run the assertions with a scrubbed environment."""
    with _hermetic():
        return _self_test()


def _self_test():
    """Assertions over the decision core, then every network path against a stub."""
    h = _Harness()
    check, raises, refuses = h.check, h.raises, h.refuses
    print("formwatch self-test: no ArcGIS, no credentials, loopback stub only")
    print("-" * 68)

    # ---- fixtures: synthetic. One seasonal form, one steady form.
    def at(text):
        return parse_when(text)

    def daily(start, days):
        base = at(start) + DAY_MS // 2          # noon UTC
        return [base + n * DAY_MS for n in range(days)]

    season = daily("2024-06-01", 76) + daily("2025-06-01", 76)   # to 15 Aug each year
    first_year = daily("2025-06-01", 76)

    # ---- time and numbers
    check(at("2026-01-15") == 1768435200000, "2026-01-15 parses to its epoch ms")
    check(at("2026-01-15T06:00:00Z") == 1768435200000 + 6 * 3600000,
          "a full timestamp parses")
    check(iso(1768435200000) == "2026-01-15T00:00:00Z", "iso formats a UTC time")
    raises(lambda: at("15/01/2026"), "a date in another layout is REFUSED")
    check(abs(utcnow_ms() - at("2026-10-13")) < 3650 * DAY_MS,
          "the real clock reads a plausible time")
    check(quantile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.95) == 10,
          "the nearest-rank 95th percentile of ten values is the largest")
    check(quantile(list(range(1, 101)), 0.95) == 95,
          "the 95th percentile of 1..100 is 95")
    check(quantile([7], 0.0) == 7, "a zero quantile does not index before the list")

    # ---- redact and urls
    check(redact("a SECRET b", "SECRET", None) == "a [redacted] b",
          "a secret is redacted and a None secret is skipped")
    check(plain_url("https://h/arcgis/rest/services/s/FeatureServer/0", True),
          "a plain layer url is accepted")
    check(plain_url("https://h/s/FeatureServer/12/", True),
          "a trailing slash and a two digit layer are accepted")
    check(not plain_url("https://h/s/FeatureServer/0?token=abc", True),
          "a url that carries a token is REFUSED  <-- pinned defect")
    check(not plain_url("https://user:pw@h/s/FeatureServer/0", True),
          "a url with credentials is REFUSED")
    check(not plain_url("https://h/s/FeatureServer/0#x", True),
          "a url with a fragment is REFUSED")
    check(not plain_url("https://h/s/FeatureServer", True),
          "the service url without a layer id is REFUSED")
    check(not plain_url("ftp://h/s/FeatureServer/0", True), "a non-http scheme is REFUSED")
    check(not plain_url("https:///s/FeatureServer/0", True), "a url with no host is REFUSED")
    check(plain_url("https://portal.example.com", False),
          "a portal url needs no layer id")
    check(is_loopback("http://127.0.0.1:5/x") and is_loopback("http://localhost/x")
          and not is_loopback("http://example.com/x"), "loopback hosts are recognised")

    # ---- layer metadata
    good = {"capabilities": "Create,Query,Update,Editing",
            "fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID"},
                       {"name": "CreationDate", "type": "esriFieldTypeDate"},
                       {"name": "Note", "type": "esriFieldTypeString"}],
            "advancedQueryCapabilities": {"supportsPagination": True,
                                          "supportsOrderBy": True}}
    problems, refusal, fields = check_layer(good)
    check(problems == [] and refusal is None, "a layer that allows Create has no problem")
    problems, _r, _f = check_layer(dict(good, capabilities="Query"))
    check(len(problems) == 1 and "lack Create" in problems[0],
          "a layer without Create is BROKEN  <-- pinned defect")
    problems, _r, _f = check_layer(dict(good, capabilities="Create, Query"))
    check(problems == [], "a space after the comma does not hide Create")
    problems, refusal, _f = check_layer({"fields": []})
    check(problems == [] and refusal is not None,
          "a layer with no capabilities key is REFUSED, not called broken")
    field, oid, paged, ordered, refusal = pick_fields(good, fields, "creationdate")
    check((field, oid, paged, ordered, refusal)
          == ("CreationDate", "OBJECTID", True, True, None),
          "the date field matches case-insensitively and paging is read")
    check(pick_fields(good, fields, "Nope")[4] is not None, "a missing date field is REFUSED")
    check("not esriFieldTypeDate" in pick_fields(good, fields, "Note")[4],
          "a field that is not a date is REFUSED")
    bare = {"fields": good["fields"][1:]}
    _p, _r, bare_fields = check_layer(dict(bare, capabilities="Create"))
    got = pick_fields(bare, bare_fields, "CreationDate")
    check(got[1] is None and got[2] is False and got[3] is False,
          "a layer with no oid or paging support reads as unordered and unpaged")
    check(check_layer({"capabilities": "Create", "fields": None})[2] == {},
          "a layer whose fields are null reads as no fields")

    # ---- the verdicts
    def verdict(stamps, now, seasonality="unknown", problems=(), refusal=None,
                complete=True, undated=0):
        return judge(list(problems), refusal, stamps, undated, complete, at(now),
                     seasonality)

    r = verdict(season, "2026-01-15")
    check(r["verdict"] == "QUIET" and r["year_ago_gap_days"] == 290.0,
          "a seasonal form in its second winter is QUIET: last year was silent too")
    r = verdict(season, "2026-06-20")
    check(r["verdict"] == "SILENT" and r["year_ago_gap_days"] == 1.0,
          "the same form with the season open and no submissions is SILENT")
    check(verdict(season, "2026-06-03")["verdict"] == "SILENT",
          "a season that opens two days late reads SILENT: a judgement call, listed "
          "in the README limits")
    r = verdict(season, "2025-10-01")
    check(r["verdict"] == "QUIET", "a seasonal form in its first autumn after a full "
          "year of history is QUIET")
    r = verdict(first_year, "2026-01-15")
    check(r["verdict"] == "REFUSED" and "first year" in r["reasons"][0],
          "a seasonal form in its first winter is REFUSED  <-- pinned defect")
    check(verdict(first_year, "2026-01-15", "none")["verdict"] == "SILENT",
          "the plain gap rule calls that same first winter SILENT, which is the "
          "misfire the refusal exists to stop  <-- pinned defect")
    check(verdict(season, "2026-01-15", "none")["verdict"] == "SILENT",
          "with --seasonality none the second winter is SILENT, because no season "
          "is allowed to explain it")

    # a season that ends on another day each year, and a stray off-season row
    early = daily("2024-06-01", 78) + daily("2025-06-01", 76)    # 2025 ends 2 days early
    for day in ("2025-10-01", "2026-01-15", "2026-05-01"):
        check(verdict(early, day)["verdict"] == "QUIET",
              "a season that ended two days earlier than last year is QUIET on %s  "
              "<-- pinned defect" % day)
    stray = (daily("2024-06-01", 76) + [at("2024-11-20") + DAY_MS // 2]
             + daily("2025-06-01", 76))
    check(verdict(stray, "2026-01-15")["verdict"] == "QUIET",
          "one off-season test row last winter does not turn this winter SILENT  "
          "<-- pinned defect")
    check(verdict(stray, "2026-05-01")["verdict"] == "SILENT",
          "a stray off-season row can turn the last weeks of the next off season SILENT: "
          "a limit, listed in the README")
    closure = daily("2024-01-01", 354) + daily("2025-01-06", 348)   # shut 20 Dec to 5 Jan
    check(verdict(closure, "2026-01-05")["verdict"] == "QUIET"
          and verdict(closure, "2026-01-10")["verdict"] == "SILENT",
          "a daily form that breaks as its yearly closure starts reads QUIET until the "
          "closure's date a year ago has passed: a limit, listed in the README")
    long_dead = daily("2022-01-01", 400)
    r = verdict(long_dead, "2026-01-15")
    check(r["verdict"] == "SILENT" and r["year_ago_gap_days"] is None,
          "a form silent for over a year is SILENT: no silence a year ago can explain it")

    # the year-ago silence must last SEASON_MATCH of this one, to the millisecond
    base = daily("2024-01-01", 400)
    holed = base[:41] + base[48:]                  # one 8 day gap at day 40.5 to 48.5
    third = 921600000                              # 32/3 days: 0.75 of it is 8 days
    check(judge([], None, holed, 0, True, holed[-1] + third, "unknown")["verdict"]
          == "QUIET", "a year-ago silence exactly 0.75 as long as this one is QUIET")
    check(judge([], None, holed, 0, True, holed[-1] + third + 1, "unknown")["verdict"]
          == "SILENT", "one millisecond over 0.75 of the year-ago silence is SILENT")

    # a submission exactly one year ago means the form was working then
    on_edge = daily("2024-01-01", 406)
    on_edge = on_edge[:41] + on_edge[48:]
    check(judge([], None, on_edge, 0, True, on_edge[41] + YEAR_DAYS * DAY_MS,
                "unknown")["verdict"] == "SILENT",
          "a submission exactly one year before now ends the year-ago silence, so "
          "this silence is SILENT  <-- pinned defect")

    # exact boundaries of the gap percentile, the first-year rule and the span rule
    gapped = [at("2025-01-01")]
    for gap in [1] * 27 + [50] + [1]:
        gapped.append(gapped[-1] + gap * DAY_MS)
    check(judge([], None, gapped, 0, True, gapped[-1] + 20 * DAY_MS, "none")["verdict"]
          == "SILENT", "the 95th percentile is taken from sorted gaps, so one long gap "
          "among 29 does not lift the limit  <-- pinned defect")
    year_plus = daily("2025-01-01", 366)
    check(judge([], None, year_plus, 0, True, year_plus[-1] + 10 * DAY_MS,
                "unknown")["verdict"] == "SILENT",
          "a history of exactly a year plus the silence is enough to judge  "
          "<-- pinned defect")
    year_short = year_plus[:-1]
    check(judge([], None, year_short, 0, True, year_short[-1] + 10 * DAY_MS,
                "unknown")["verdict"] == "REFUSED",
          "a history one day short of a year is REFUSED however long the silence")
    span = daily("2025-01-01", 28) + [at("2025-01-29") + DAY_MS // 2] * 2
    check(judge([], None, span, 0, True, span[-1] + DAY_MS, "none")["verdict"] == "QUIET",
          "a history spanning exactly 28 days is enough for --seasonality none  "
          "<-- pinned defect")
    short = span[:-2] + [span[-3] + DAY_MS - 1000] * 2
    check(judge([], None, short, 0, True, short[-1] + DAY_MS, "none")["verdict"]
          == "REFUSED",
          "a history spanning under 28 days is REFUSED for --seasonality none")

    steady = daily("2025-01-01", 400)
    last = steady[-1]

    def steady_at(delta):
        return judge([], None, steady, 0, True, last + delta, "none")

    check(steady_at(2 * DAY_MS)["verdict"] == "QUIET", "a daily form silent two days is QUIET")
    check(steady_at(3 * DAY_MS)["verdict"] == "QUIET",
          "silence equal to the limit is QUIET: the test is strictly greater")
    check(steady_at(3 * DAY_MS + 1)["verdict"] == "SILENT",
          "one millisecond past the limit is SILENT")
    check(steady_at(3 * DAY_MS)["limit_days"] == 3.0, "the limit is 3 times the 95th "
          "percentile gap of one day")
    check(steady_at(-DAY_MS)["verdict"] == "REFUSED"
          and "after now" in steady_at(-DAY_MS)["reasons"][0],
          "a newest submission dated after now is REFUSED")
    check(judge([], None, steady, 0, True, last + 20 * DAY_MS, "unknown")["verdict"]
          == "SILENT", "a steady form with a year of history and 20 silent days is SILENT")
    hourly = [at("2026-01-01") + n * 3600000 for n in range(960)]
    check(judge([], None, hourly, 0, True, hourly[-1] + 12 * 3600000, "none")["verdict"]
          == "QUIET", "twelve quiet hours in an hourly form are QUIET: the limit has "
          "a floor of one day")
    check(judge([], None, hourly, 0, True, hourly[-1] + 25 * 3600000, "none")["verdict"]
          == "SILENT", "twenty five quiet hours in an hourly form are SILENT")
    r = judge([], None, steady, 0, True, last, "unknown")
    check(r["verdict"] == "QUIET" and r["year_ago_gap_days"] is None,
          "a submission this minute is QUIET and needs no year-ago comparison  "
          "<-- pinned defect")
    burst = [at("2026-03-01") + n * 1000 for n in range(40)]
    r = judge([], None, burst, 0, True, at("2026-03-15"), "none")
    check(r["verdict"] == "REFUSED" and "spans" in r["reasons"][0],
          "forty submissions in forty seconds are REFUSED: no cadence")
    sparse = daily("2025-01-01", 29)
    check(judge([], None, sparse, 0, True, at("2025-03-01"), "none")["verdict"] == "REFUSED",
          "29 submissions are under the minimum and REFUSED")
    check(judge([], None, steady[:30], 0, True, steady[29] + DAY_MS, "none")["verdict"]
          == "QUIET", "exactly 30 submissions are enough")
    check(judge([], None, [], 0, True, 0, "none")["verdict"] == "REFUSED",
          "no submission at all is REFUSED")
    check(judge([], None, steady, 0, False, last, "none")["verdict"] == "REFUSED",
          "an incomplete read is REFUSED  <-- pinned defect")
    check(judge([], "no field", steady, 0, True, last, "none")["verdict"] == "REFUSED",
          "a layer refusal reaches the verdict")
    check(judge(["no Create"], "no field", steady, 0, True, last, "none")["verdict"]
          == "BROKEN", "BROKEN outranks REFUSED: a missing Create is a fact")
    check(judge(["no Create"], None, [], 0, True, 0, "none")["verdict"] == "BROKEN"
          and EXIT["BROKEN"] == 1, "BROKEN needs no history at all")
    check(judge([], None, list(reversed(steady)), 0, True, last, "none")["verdict"]
          == "QUIET", "an unsorted history is sorted before it is read")
    check([EXIT[v] for v in ("QUIET", "SILENT", "BROKEN", "REFUSED")] == [0, 1, 1, 4],
          "exit codes: 0 quiet, 1 silent or broken, 4 refused")
    text = "\n".join(render(verdict(season, "2026-01-15"), "https://h/FeatureServer/0"))
    check("QUIET" in text and "152 dated" in text and "last 2025-08-15" in text,
          "the report names the verdict and the last submission")
    check("submissions" not in "\n".join(render(
        judge([], None, [], 0, True, 0, "none"), "u")),
          "a refused report with no history prints no submissions line")

    # ---- parser
    refuses(["--ap"], "a unique prefix --ap of --apply is REFUSED by the parser  "
                      "<-- pinned defect")
    refuses(["--layer", "https://h/FeatureServer/0"],
            "a prefix --layer of --layer-url is REFUSED by the parser")
    refuses(["--self"], "a prefix --self of --self-test is REFUSED by the parser")
    refuses(["--seasonality", "maybe"], "an unknown seasonality is REFUSED")
    noise, sys.stderr = sys.stderr, io.StringIO()
    try:
        _parse(["--ap"])
    except SystemExit as exc:
        status = exc.code
    finally:
        sys.stderr = noise
    check(status == 64, "a parser refusal exits 64, not argparse's 2, which means a "
          "failed read here  <-- pinned defect")
    check(_parse(["--apply", "--out", "x"]).apply is True, "the full --apply is accepted")
    check(_parse([]).apply is False and _parse([]).seasonality == "unknown",
          "--apply is off and seasonality is unknown by default")

    # ---- usage errors, no network
    def usage(argv, env=None):
        code, out, err = _run_main(argv, env)
        return code, err

    code, err = usage([])
    check(code == 64 and "--layer-url is required" in err, "no layer url is a usage error")
    layer = "http://127.0.0.1:9/svc/FeatureServer/0"
    check(usage(["--layer-url", layer + "?token=abc"])[0] == 64
          and "abc" not in usage(["--layer-url", layer + "?token=abc"])[1],
          "a token in the url is a usage error and is not echoed  <-- pinned defect")
    check(usage(["--layer-url", layer, "--max-rows", "0"])[0] == 64, "--max-rows 0 is refused")
    check(usage(["--layer-url", layer, "--apply"])[0] == 64, "--apply without --out is refused")
    check(usage(["--layer-url", layer, "--now", "tomorrow"])[0] == 64, "a bad --now is refused")
    check(usage(["--layer-url", layer, "--username", "u"])[0] == 64,
          "--username without --portal is refused")
    check(usage(["--layer-url", layer, "--username", "u", "--portal",
                 "http://example.com"])[0] == 64,
          "a password over plain http to a remote host is refused")
    check(usage(["--layer-url", layer, "--username", "u", "--portal",
                 "https://example.com/?token=abc"])[0] == 64,
          "a portal url that carries a token is refused")
    check(usage(["--layer-url", layer, "--username", "u", "--portal",
                 "https://example.com"], {TOKEN_ENV: "T"})[0] == 64,
          "a token and a username together are refused")

    # ---- network paths against the stub
    rows = [{"OBJECTID": i + 1, "CreationDate": ms} for i, ms in enumerate(season)]
    tmp = tempfile.mkdtemp(prefix="formwatch-")
    state = {"layer": good, "rows": rows, "log": [], "token": None,
             "login": ("builder", "hunter2-pw")}
    server, base = _start_stub(state)
    url = base + "/svc/FeatureServer/0"
    try:
        def go(extra=(), env=None, **over):
            state.update(over)
            del state["log"][:]
            return _run_main(["--layer-url", url] + list(extra), env)

        def queries():
            return [e for e in state["log"] if e[0].endswith("/query")]

        code, out, err = go(["--now", "2026-01-15"])
        check(code == 0 and "QUIET" in out, "stub: a seasonal second winter exits 0 QUIET")
        check(len(queries()) == 1 and queries()[0][1]["orderByFields"] == "OBJECTID"
              and queries()[0][1]["resultOffset"] == "0",
              "stub: rows are read in object id order from offset 0")
        check(all("?" not in path for path, _f in state["log"]),
              "stub: no request carries a query string")
        code, out, err = go(["--now", "2026-06-20"])
        check(code == 1 and "SILENT" in out, "stub: an open season with no rows exits 1 SILENT")
        code, out, err = go(["--now", "2026-01-15", "--seasonality", "none"])
        check(code == 1, "stub: --seasonality none reaches the verdict")
        code, out, err = go(["--now", "2026-01-15", "--date-field", "creationdate"])
        check(code == 0, "stub: the date field name is case-insensitive")

        state["rows"] = [{"OBJECTID": i + 1, "CreationDate": ms}
                         for i, ms in enumerate(first_year)]
        code, out, err = go(["--now", "2026-01-15"])
        check(code == 4 and "first year" in out,
              "stub: a first-year seasonal form exits 4 REFUSED  <-- pinned defect")

        state["rows"] = rows
        code, out, err = go(["--now", "2026-01-15"], page_cap=7, layer=dict(good, maxRecordCount=50))
        check(code == 0 and len(queries()) == 22,
              "stub: a layer that returns 7 rows per page is read in 22 pages")
        check([int(q[1]["resultOffset"]) for q in queries()][:3] == [0, 7, 14],
              "stub: the offset advances by the rows returned, not the rows asked")
        check(queries()[0][1]["resultRecordCount"] == "50",
              "stub: the page size is lowered to maxRecordCount")
        state.pop("page_cap")

        code, out, err = go(["--now", "2026-01-15"], layer=dict(good, capabilities="Query"))
        check(code == 1 and "BROKEN" in out and queries() == [],
              "stub: a layer without Create is BROKEN and no row is read  "
              "<-- pinned defect")
        unpaged = dict(good, advancedQueryCapabilities={})
        state["rows"] = rows[:40]
        code, out, err = go(["--now", "2026-01-15", "--seasonality", "none"],
                            layer=unpaged)
        check(code == 1 and len(queries()) == 1 and "resultOffset" not in queries()[0][1]
              and "orderByFields" not in queries()[0][1],
              "stub: a layer without paging is read in one request with no offset")
        small = dict(unpaged, maxRecordCount=0)
        state["rows"] = rows
        state["page_cap"] = 10
        code, out, err = go(["--now", "2026-01-15"], layer=small)
        check(code == 4 and "incomplete" in out,
              "stub: an unpaged layer that truncates is REFUSED, not judged")
        state.pop("page_cap")
        undated_rows = rows[:40] + [{"OBJECTID": 900, "CreationDate": None}]
        code, out, err = go(["--now", "2026-01-15", "--seasonality", "none"],
                            layer=good, rows=undated_rows)
        check("1 undated" in out, "stub: a row with no date is counted, not read as a time")
        state["page_cap"] = 40
        code, out, err = go(["--now", "2026-01-15", "--max-rows", "50"], rows=rows,
                            layer=good)
        state.pop("page_cap")
        check(code == 4 and "incomplete" in out,
              "stub: a history cut by --max-rows is REFUSED")
        code, out, err = go(["--now", "2026-01-15"], mode="endless")
        check(code == 4, "stub: an endless empty page stops and is REFUSED")
        code, out, err = go(["--now", "2026-01-15", "--max-rows", "10"], mode="loop")
        check(code == 4, "stub: a layer that repeats a full page forever stops at --max-rows")
        state["mode"] = None
        code, out, err = go(["--now", "2026-01-15", "--date-field", "Nope"])
        check(code == 4 and "no field named Nope" in out, "stub: a wrong date field is REFUSED")
        code, out, err = go(["--now", "2026-01-15"], rows=[{"OBJECTID": 1, "CreationDate": "x"}])
        check(code == 2 and "not a date" in err, "stub: a text value in the date field exits 2")
        state["rows"] = rows

        for mode in ("http500", "garbage", "error200", "errorstr", "notlayer", "nofeatures"):
            code, out, err = go(["--now", "2026-01-15"], mode=mode)
            check(code == 2 and err.startswith("error: "),
                  "stub: fault %s exits 2 with an error line" % mode)
        state["mode"] = None

        # ---- credentials: nothing may reach an output stream or a file
        state["token"] = "TOKEN-9f3a-live"
        code, out, err = go(["--now", "2026-01-15"])
        check(code == 2 and "498" in err, "stub: a missing token exits 2 with the service error")
        code, out, err = go(["--now", "2026-01-15"], {TOKEN_ENV: "TOKEN-9f3a-live"})
        check(code == 0, "stub: a token from the environment is accepted")
        check(all(f.get("token") == "TOKEN-9f3a-live" for _p, f in state["log"]),
              "stub: the token travels in the body of every request")
        code, out, err = go(["--now", "2026-01-15"], {TOKEN_ENV: "wrong-TOKEN-zz"})
        check(code == 2 and "wrong-TOKEN-zz" not in out + err,
              "stub: a rejected token is not printed back  <-- pinned defect")
        portal = ["--portal", base, "--username", "builder"]
        code, out, err = go(["--now", "2026-01-15"] + portal, {PASSWORD_ENV: "hunter2-pw"})
        check(code == 0 and "hunter2-pw" not in out + err and "TOKEN-9f3a" not in out + err,
              "stub: username and password get a token, and neither is printed")
        check(state["log"][0][0].endswith("/sharing/rest/generateToken")
              and state["log"][0][1]["referer"] == base
              and state["log"][0][1]["expiration"] == "30"
              and state["log"][0][1]["client"] == "referer",
              "stub: generateToken is called first with client and referer")
        state["login"] = ("builder", "other-pw")
        code, out, err = go(["--now", "2026-01-15"] + portal, {PASSWORD_ENV: "hunter2-pw"})
        check(code == 2 and "hunter2-pw" not in out + err,
              "stub: a refused login exits 2 without echoing the password")
        state["login"] = ("builder", "hunter2-pw")
        state["token"] = ""
        code, out, err = go(["--now", "2026-01-15"] + portal, {PASSWORD_ENV: "hunter2-pw"})
        check(code == 2 and "no token" in err, "stub: a login that returns no token exits 2")
        state["token"] = "TOKEN-9f3a-live"
        saved_getpass = getpass.getpass
        getpass.getpass = lambda prompt: "hunter2-pw"
        try:
            code, out, err = go(["--now", "2026-01-15"] + portal)
        finally:
            getpass.getpass = saved_getpass
        check(code == 0, "stub: with no password in the environment it prompts")

        code, out, err = go(["--now", "2026-01-15"], {TOKEN_ENV: "TOKEN-9f3a-live"},
                            mode="echo")
        check(code == 2 and "TOKEN-9f3a" not in out + err and "[redacted]" in err,
              "stub: a service that echoes the token in its error does not leak it  "
              "<-- pinned defect")
        state["mode"] = None
        leaked = True
        try:
            _call("http://127.0.0.1:1/TOKEN-9f3a-live", {}, token="TOKEN-9f3a-live")
        except RuntimeError as exc:
            leaked = "TOKEN-9f3a" in str(exc)
        check(not leaked, "a transport error that quotes the url does not leak the token")

        # ---- the one write
        report = os.path.join(tmp, "verdict.json")
        env = {TOKEN_ENV: "TOKEN-9f3a-live"}
        code, out, err = go(["--now", "2026-01-15", "--out", report], env)
        check(code == 0 and not os.path.exists(report) and "No report was written" in out,
              "without --apply nothing is written")
        code, out, err = go(["--now", "2026-01-15", "--out", report, "--apply"], env)
        with io.open(report, encoding="utf-8") as handle:
            written = handle.read()
        check(code == 0 and json.loads(written)["verdict"] == "QUIET",
              "with --apply the verdict is written as JSON")
        check("TOKEN-9f3a" not in written and "hunter2" not in written,
              "the report holds no credential")
        keys = list(json.loads(written))
        check(keys == sorted(keys) and "year_ago_gap_days" in keys,
              "the report lists its keys in sorted order")
        code, out, err = go(["--now", "2026-01-15", "--out", report, "--apply"], env)
        check(code == 2 and "exists" in err, "an existing report is never overwritten")
        check(sorted(os.listdir(tmp)) == ["verdict.json"],
              "the run left exactly one file behind")

        # ---- the crash path: never reported as a finding
        saved_read = globals()["read_layer"]
        def crash(*args):
            raise KeyError("TOKEN-9f3a-live")

        globals()["read_layer"] = crash
        try:
            code, out, err = go(["--now", "2026-01-15"], env)
        finally:
            globals()["read_layer"] = saved_read
        check(code == 70 and "not a finding" in err and "TOKEN-9f3a" not in err,
              "an unexpected crash exits 70 with the token redacted")

        # ---- the caller's own credentials and proxy never reach the stub
        state["token"] = None
        outer = {TOKEN_ENV: "REAL-outer-token", PASSWORD_ENV: "REAL-outer-pw",
                 "HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9"}
        os.environ.update(outer)
        try:
            with _hermetic():
                bare_env = (TOKEN_ENV not in os.environ and PASSWORD_ENV not in os.environ
                            and "HTTP_PROXY" not in os.environ
                            and urllib.request.getproxies() == {"no": "*"})
                code, out, err = go(["--now", "2026-01-15"])
            restored = os.environ.get(TOKEN_ENV) == "REAL-outer-token"
        finally:
            for name in outer:
                os.environ.pop(name, None)
        check(bare_env and restored, "the self-test environment holds no token, no "
              "password and no proxy, and gives them back afterwards  <-- pinned defect")
        check(code == 0 and state["log"] and all("token" not in f for _p, f in state["log"]),
              "a token and a proxy in the caller's environment never reach the stub  "
              "<-- pinned defect")

        # ---- the real clock, with no --now
        state["rows"] = [{"OBJECTID": i + 1, "CreationDate": ms}
                         for i, ms in enumerate(daily("2001-01-01", 700))]
        code, out, err = go([])
        check(code == 1 and "SILENT" in out,
              "stub: against the real clock a form dead for decades is SILENT")
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(tmp)
    code, out, err = _run_main(["--layer-url", url, "--now", "2026-01-15"])
    check(code == 2 and err.startswith("error: "),
          "a service that is not listening exits 2")

    # ---- the harness itself, so a broken tool turns this self-test red
    probe = _Harness()
    with contextlib.redirect_stdout(io.StringIO()) as noise:
        probe.check(False, "a false check")
        probe.raises(lambda: None, "no error")
        probe.raises(lambda: 1 / 0, "wrong error")
        probe.refuses(["--layer-url", "u"], "an argv argparse accepts")
    check(len(probe.failed) == 4 and probe.passed == 0
          and noise.getvalue().count("FAIL  ") == 4,
          "the harness records a false check, a missing exception, a wrong "
          "exception and an accepted argv as four printed failures  "
          "<-- pinned defect")
    code, lines = _footer(probe)
    check(code == 1 and lines[0] == "4 assertions, 4 failed"
          and lines[1] == "  FAILED: a false check",
          "a red harness reports its failures and returns 1")
    namespace = runpy.run_path(os.path.abspath(__file__), run_name="formwatch_probe")
    check("main" in namespace and callable(namespace["main"]),
          "importing the file defines main and does not run it")

    print("-" * 68)
    code, lines = _footer(h)
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
