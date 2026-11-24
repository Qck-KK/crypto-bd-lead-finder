"""Buyer feedback: a signed link in every report, a small form, and suppression of bad leads.

- The link carries the job's reference and an HMAC of it (FEEDBACK_SECRET), so only someone
  holding the report can rate that job. No account is needed.
- Buyers give 1-5 stars and may mark each lead: useful, not useful, left the company, wrong person.
- Leads marked "left the company" or "wrong person" are reports; a person reported by at least
  SUPPRESS_MIN_REPORTS different orders within SUPPRESS_TTL_DAYS is left out of later deliveries
  (suppression, see app/suppression.py).
- Storage holds one-way person hashes and titles, not names; comments are capped at 500 characters.
"""

import hashlib
import hmac
import html
import json
import logging
import time
from collections import defaultdict, deque
from typing import Optional
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .suppression import SUPPRESS_MIN_REPORTS, SUPPRESS_TTL_DAYS, Report, Suppression

logger = logging.getLogger(__name__)

LABELS = {"useful": "Useful", "not_useful": "Not useful", "left_company": "Left the company",
          "wrong_person": "Wrong person"}
SUPPRESS_LABELS = {"left_company", "wrong_person"}
MAX_BODY = 8192
RATE_LIMIT = (20, 60.0)  # requests per window (seconds) per client address


def sign(ref: str, secret: str) -> str:
    return hmac.new(secret.encode(), ref.encode(), hashlib.sha256).hexdigest()[:32]


def feedback_link(public_url: str, ref: str, secret: str) -> Optional[str]:
    if not (public_url and secret):
        return None
    return f"{public_url}/feedback?ref={quote(ref)}&t={sign(ref, secret)}"


def _suppress_indexes(labels: dict, n_leads: int) -> dict[int, str]:
    return {int(i): v for i, v in labels.items() if v in SUPPRESS_LABELS and 0 <= int(i) < n_leads}


class MemoryFeedbackStore:
    """For local runs and tests; production uses PostgresFeedbackStore.

    Each order's suppressing marks are its reports (one per lead index). Resubmitting the form does not count
    twice; taking a mark back withdraws that report. `suppressed` holds rows of the pre-report format
    (hash -> reason, reports, updated_at), read but no longer written."""

    def __init__(self, min_reports: int = SUPPRESS_MIN_REPORTS, ttl_days: float = SUPPRESS_TTL_DAYS):
        self.min_reports, self.ttl_days = min_reports, ttl_days
        self.deliveries: dict[str, dict] = {}
        self.feedback: dict[str, dict] = {}
        self.reports: dict[tuple[str, int], dict] = {}
        self.suppressed: dict[str, dict] = {}

    async def remember_delivery(self, ref: str, company: str, leads: list[dict]) -> None:
        self.deliveries[ref] = {"company": company, "leads": leads, "created_at": time.time()}

    async def get_delivery(self, ref: str) -> Optional[dict]:
        return self.deliveries.get(ref)

    async def save_feedback(self, ref: str, rating: int, labels: dict[int, str], comment: str) -> None:
        delivery = self.deliveries[ref]
        previous = self.feedback.get(ref, {}).get("labels", {})
        already = set(_suppress_indexes(previous, len(delivery["leads"])))
        self.feedback[ref] = {"rating": rating, "labels": labels, "comment": comment, "at": time.time()}
        wanted = _suppress_indexes(labels, len(delivery["leads"]))
        for key in [k for k in self.reports if k[0] == ref and k[1] not in wanted]:
            del self.reports[key]
        for idx, label in wanted.items():
            if idx not in already:
                lead = delivery["leads"][idx]
                self.reports[(ref, idx)] = {"hash": lead["hash"], "full_hash": lead.get("full_hash"),
                                            "reason": label, "at": time.time()}

    async def suppression(self, now: Optional[float] = None) -> Suppression:
        cutoff = (time.time() if now is None else now) - self.ttl_days * 86400
        reports = [Report(ref, r["hash"], r.get("full_hash")) for (ref, _), r in self.reports.items()
                   if r["at"] > cutoff]
        for h, entry in self.suppressed.items():
            if entry.get("updated_at", time.time()) > cutoff:
                reports += _legacy_reports(h, entry["reports"])
        return Suppression(reports, self.min_reports)

    async def summary(self) -> dict:
        ratings = [f["rating"] for f in self.feedback.values()]
        labels = defaultdict(int)
        for f in self.feedback.values():
            for label in f["labels"].values():
                labels[label] += 1
        return {"deliveries": len(self.deliveries), "responses": len(ratings),
                "avg_rating": round(sum(ratings) / len(ratings), 2) if ratings else None,
                "labels": dict(labels), "suppressed_people": (await self.suppression()).active_count(),
                "suppression_reports": len(self.reports)}


def _legacy_reports(hash_: str, count: int) -> list[Report]:
    """A pre-report suppressed_people row: `count` reports from orders we can no longer tell apart (each was
    counted once per order), on the coarse hash only."""
    return [Report(f"legacy:{hash_}:{i}", hash_, None) for i in range(max(0, int(count)))]


class PostgresFeedbackStore(MemoryFeedbackStore):
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS feedback_deliveries (ref TEXT PRIMARY KEY, company TEXT NOT NULL,
        leads JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
    CREATE TABLE IF NOT EXISTS feedback (ref TEXT PRIMARY KEY REFERENCES feedback_deliveries(ref),
        rating INT NOT NULL, labels JSONB NOT NULL, comment TEXT NOT NULL DEFAULT '',
        submitted_at TIMESTAMPTZ NOT NULL DEFAULT now());
    CREATE TABLE IF NOT EXISTS suppressed_people (hash TEXT PRIMARY KEY, reason TEXT NOT NULL,
        reports INT NOT NULL DEFAULT 1, updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
    CREATE TABLE IF NOT EXISTS suppression_reports (ref TEXT NOT NULL REFERENCES feedback_deliveries(ref),
        lead_index INT NOT NULL, hash TEXT NOT NULL, full_hash TEXT, reason TEXT NOT NULL,
        reported_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (ref, lead_index));
    CREATE INDEX IF NOT EXISTS suppression_reports_at_idx ON suppression_reports (reported_at);
    """
    # suppressed_people is the format before per-order reports: still read (its rows count as `reports` reports
    # on the coarse hash, within the TTL of updated_at), no longer written, so a rollback finds it unchanged.

    def __init__(self, dsn: str, min_reports: int = SUPPRESS_MIN_REPORTS, ttl_days: float = SUPPRESS_TTL_DAYS):
        super().__init__(min_reports, ttl_days)
        self._dsn, self._pool = dsn, None

    async def _p(self):
        if self._pool is None:
            import asyncpg

            from .ledger import SCHEMA_LOCK_KEY

            pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=3)
            async with pool.acquire() as conn:
                # Both agents share these tables and start together: serialize the migration like the ledger.
                await conn.execute("SELECT pg_advisory_lock($1)", SCHEMA_LOCK_KEY)
                try:
                    await conn.execute(self.SCHEMA)
                finally:
                    await conn.execute("SELECT pg_advisory_unlock($1)", SCHEMA_LOCK_KEY)
            self._pool = pool
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def remember_delivery(self, ref: str, company: str, leads: list[dict]) -> None:
        pool = await self._p()
        await pool.execute("INSERT INTO feedback_deliveries (ref, company, leads) VALUES ($1, $2, $3::jsonb) "
                           "ON CONFLICT (ref) DO UPDATE SET leads = EXCLUDED.leads", ref, company, json.dumps(leads))

    async def get_delivery(self, ref: str) -> Optional[dict]:
        pool = await self._p()
        row = await pool.fetchrow("SELECT company, leads FROM feedback_deliveries WHERE ref = $1", ref)
        return {"company": row["company"], "leads": json.loads(row["leads"])} if row else None

    async def save_feedback(self, ref: str, rating: int, labels: dict[int, str], comment: str) -> None:
        pool = await self._p()
        delivery = await self.get_delivery(ref)
        n = len(delivery["leads"])
        async with pool.acquire() as conn, conn.transaction():
            previous = await conn.fetchval("SELECT labels FROM feedback WHERE ref = $1 FOR UPDATE", ref)
            await conn.execute(
                "INSERT INTO feedback (ref, rating, labels, comment) VALUES ($1, $2, $3::jsonb, $4) "
                "ON CONFLICT (ref) DO UPDATE SET rating = $2, labels = $3::jsonb, comment = $4, submitted_at = now()",
                ref, rating, json.dumps({str(k): v for k, v in labels.items()}), comment)
            # Marks this order had already made: not counted again. A mark made before per-order reports existed
            # is still counted in suppressed_people, so it gets no report row either.
            already = set(_suppress_indexes(json.loads(previous), n)) if previous else set()
            wanted = _suppress_indexes(labels, n)
            await conn.execute("DELETE FROM suppression_reports WHERE ref = $1 AND NOT (lead_index = ANY($2::int[]))",
                               ref, list(wanted))
            for idx, label in wanted.items():
                if idx not in already:
                    lead = delivery["leads"][idx]
                    await conn.execute(
                        "INSERT INTO suppression_reports (ref, lead_index, hash, full_hash, reason) "
                        "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (ref, lead_index) DO NOTHING",
                        ref, idx, lead["hash"], lead.get("full_hash"), label)

    async def suppression(self, now: Optional[float] = None) -> Suppression:
        """Reports younger than the TTL. `now` is accepted for the memory store's interface; the database
        clock decides here."""
        pool = await self._p()
        rows = await pool.fetch("SELECT ref, hash, full_hash FROM suppression_reports "
                                "WHERE reported_at > now() - make_interval(secs => $1)", self.ttl_days * 86400.0)
        reports = [Report(r["ref"], r["hash"], r["full_hash"]) for r in rows]
        for r in await pool.fetch("SELECT hash, reports FROM suppressed_people "
                                  "WHERE updated_at > now() - make_interval(secs => $1)", self.ttl_days * 86400.0):
            reports += _legacy_reports(r["hash"], r["reports"])
        return Suppression(reports, self.min_reports)

    async def summary(self) -> dict:
        pool = await self._p()
        row = await pool.fetchrow("SELECT (SELECT count(*) FROM feedback_deliveries) d, count(*) n, avg(rating) a "
                                  "FROM feedback")
        labels = defaultdict(int)
        for r in await pool.fetch("SELECT labels FROM feedback"):
            for label in json.loads(r["labels"]).values():
                labels[label] += 1
        reports = await pool.fetchval("SELECT count(*) FROM suppression_reports")
        return {"deliveries": row["d"], "responses": row["n"],
                "avg_rating": round(float(row["a"]), 2) if row["a"] is not None else None,
                "labels": dict(labels), "suppressed_people": (await self.suppression()).active_count(),
                "suppression_reports": reports}


_store: MemoryFeedbackStore = MemoryFeedbackStore()


def get_store() -> MemoryFeedbackStore:
    return _store


def set_store(store: MemoryFeedbackStore) -> None:
    global _store
    _store = store


# --- HTTP -------------------------------------------------------------------

_hits: dict[str, deque] = defaultdict(deque)


def _rate_limited(client: str) -> bool:
    limit, window = RATE_LIMIT
    now = time.monotonic()
    q = _hits[client]
    while q and now - q[0] > window:
        q.popleft()
    q.append(now)
    return len(q) > limit


def _page(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Lead feedback</title><style>body{font:16px system-ui,sans-serif;max-width:640px;margin:2rem auto;"
        "padding:0 16px;color:#111;background:#fff}fieldset{border:1px solid #ddd;border-radius:8px;margin:12px 0}"
        "label{margin-right:12px}button{font:inherit;padding:8px 16px}textarea{width:100%}</style>" + body,
        status_code=status)


def add_feedback_routes(app: FastAPI, secret: str, admin_token: str = "") -> None:
    def check(ref: str, token: str) -> None:
        if not secret or not ref or not hmac.compare_digest(sign(ref, secret), token or ""):
            raise HTTPException(status_code=403, detail="invalid feedback link")

    @app.get("/feedback", response_class=HTMLResponse, include_in_schema=False)
    async def feedback_form(request: Request, ref: str = "", t: str = ""):
        if _rate_limited(request.client.host if request.client else "?"):
            raise HTTPException(status_code=429, detail="too many requests")
        check(ref, t)
        delivery = await get_store().get_delivery(ref)
        if not delivery:
            raise HTTPException(status_code=404, detail="unknown job")
        rows = []
        for i, lead in enumerate(delivery["leads"]):
            opts = "".join(f"<label><input type='radio' name='lead_{i}' value='{k}'> {v}</label>"
                           for k, v in LABELS.items())
            rows.append(f"<fieldset><legend>#{i + 1} {html.escape(lead.get('title') or 'n/a')}</legend>{opts}"
                        "</fieldset>")
        stars = "".join(f"<label><input type='radio' name='rating' value='{n}' required> {n}</label>"
                        for n in range(1, 6))
        return _page(
            f"<h1>How useful were these leads?</h1><p>{html.escape(delivery['company'])}: "
            f"{len(delivery['leads'])} leads. Numbers match the report.</p>"
            f"<form method='post' action='/feedback'><input type='hidden' name='ref' value='{html.escape(ref)}'>"
            f"<input type='hidden' name='t' value='{html.escape(t)}'><fieldset><legend>Overall (1-5)</legend>"
            f"{stars}</fieldset>{''.join(rows)}<p><label for='c'>Comment (optional)</label>"
            "<textarea id='c' name='comment' maxlength='500' rows='3'></textarea></p>"
            "<button type='submit'>Send feedback</button></form>")

    @app.post("/feedback", include_in_schema=False)
    async def feedback_submit(request: Request):
        if _rate_limited(request.client.host if request.client else "?"):
            raise HTTPException(status_code=429, detail="too many requests")
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise HTTPException(status_code=413, detail="too large")
        is_json = request.headers.get("content-type", "").startswith("application/json")
        if is_json:
            try:
                data = json.loads(raw or b"{}")
            except ValueError:
                raise HTTPException(status_code=400, detail="invalid JSON")
            labels_in = {int(k): v for k, v in (data.get("labels") or {}).items() if str(k).isdigit()}
        else:
            form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
            data = form
            labels_in = {int(k[5:]): v for k, v in form.items() if k.startswith("lead_") and k[5:].isdigit()}
        ref, token = str(data.get("ref", "")), str(data.get("t", ""))
        check(ref, token)
        delivery = await get_store().get_delivery(ref)
        if not delivery:
            raise HTTPException(status_code=404, detail="unknown job")
        try:
            rating = int(data.get("rating"))
        except (TypeError, ValueError):
            rating = 0
        if not 1 <= rating <= 5:
            raise HTTPException(status_code=400, detail="rating must be 1-5")
        labels = {i: v for i, v in labels_in.items() if v in LABELS and 0 <= i < len(delivery["leads"])}
        comment = str(data.get("comment") or "")[:500]
        await get_store().save_feedback(ref, rating, labels, comment)
        logger.info("Feedback for %s: rating=%d labels=%d", ref[:8], rating, len(labels))
        if is_json:
            return JSONResponse({"status": "ok"})
        return _page("<h1>Thank you</h1><p>Your feedback was saved. People reported as gone or wrong by "
                     f"{SUPPRESS_MIN_REPORTS} or more orders are left out of later deliveries for "
                     f"{SUPPRESS_TTL_DAYS} days.</p>")

    @app.get("/feedback/summary", include_in_schema=False)
    async def feedback_summary(request: Request):
        if not admin_token or not hmac.compare_digest(request.headers.get("x-admin-token", ""), admin_token):
            raise HTTPException(status_code=403, detail="forbidden")
        return await get_store().summary()
