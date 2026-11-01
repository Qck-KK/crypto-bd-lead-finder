"""Buyer feedback: a signed link in every report, a small form, and suppression of bad leads.

- The link carries the job's reference and an HMAC of it (FEEDBACK_SECRET), so only someone
  holding the report can rate that job. No account is needed.
- Buyers give 1-5 stars and may mark each lead: useful, not useful, left the company, wrong person.
- Leads marked "left the company" or "wrong person" are never delivered again (suppression).
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


class MemoryFeedbackStore:
    """For local runs and tests; production uses PostgresFeedbackStore."""

    def __init__(self):
        self.deliveries: dict[str, dict] = {}
        self.feedback: dict[str, dict] = {}
        self.suppressed: dict[str, dict] = {}

    async def remember_delivery(self, ref: str, company: str, leads: list[dict]) -> None:
        self.deliveries[ref] = {"company": company, "leads": leads, "created_at": time.time()}

    async def get_delivery(self, ref: str) -> Optional[dict]:
        return self.deliveries.get(ref)

    async def save_feedback(self, ref: str, rating: int, labels: dict[int, str], comment: str) -> None:
        delivery = self.deliveries[ref]
        previous = self.feedback.get(ref, {}).get("labels", {})
        already = {i for i, v in previous.items() if v in SUPPRESS_LABELS}
        self.feedback[ref] = {"rating": rating, "labels": labels, "comment": comment, "at": time.time()}
        for idx, label in labels.items():
            if label in SUPPRESS_LABELS and idx not in already and 0 <= idx < len(delivery["leads"]):
                h = delivery["leads"][idx]["hash"]
                entry = self.suppressed.setdefault(h, {"reason": label, "reports": 0})
                entry["reports"] += 1

    async def suppressed_hashes(self) -> frozenset:
        return frozenset(self.suppressed)

    async def summary(self) -> dict:
        ratings = [f["rating"] for f in self.feedback.values()]
        labels = defaultdict(int)
        for f in self.feedback.values():
            for label in f["labels"].values():
                labels[label] += 1
        return {"deliveries": len(self.deliveries), "responses": len(ratings),
                "avg_rating": round(sum(ratings) / len(ratings), 2) if ratings else None,
                "labels": dict(labels), "suppressed_people": len(self.suppressed)}


class PostgresFeedbackStore(MemoryFeedbackStore):
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS feedback_deliveries (ref TEXT PRIMARY KEY, company TEXT NOT NULL,
        leads JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
    CREATE TABLE IF NOT EXISTS feedback (ref TEXT PRIMARY KEY REFERENCES feedback_deliveries(ref),
        rating INT NOT NULL, labels JSONB NOT NULL, comment TEXT NOT NULL DEFAULT '',
        submitted_at TIMESTAMPTZ NOT NULL DEFAULT now());
    CREATE TABLE IF NOT EXISTS suppressed_people (hash TEXT PRIMARY KEY, reason TEXT NOT NULL,
        reports INT NOT NULL DEFAULT 1, updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
    """

    def __init__(self, dsn: str):
        super().__init__()
        self._dsn, self._pool = dsn, None

    async def _p(self):
        if self._pool is None:
            import asyncpg

            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=3)
            async with self._pool.acquire() as conn:
                await conn.execute(self.SCHEMA)
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
        async with pool.acquire() as conn, conn.transaction():
            previous = await conn.fetchval("SELECT labels FROM feedback WHERE ref = $1", ref)
            await conn.execute(
                "INSERT INTO feedback (ref, rating, labels, comment) VALUES ($1, $2, $3::jsonb, $4) "
                "ON CONFLICT (ref) DO UPDATE SET rating = $2, labels = $3::jsonb, comment = $4, submitted_at = now()",
                ref, rating, json.dumps({str(k): v for k, v in labels.items()}), comment)
            already = {int(k) for k, v in json.loads(previous).items() if v in SUPPRESS_LABELS} if previous else set()
            for idx, label in labels.items():
                if label in SUPPRESS_LABELS and idx not in already and 0 <= idx < len(delivery["leads"]):
                    await conn.execute(
                        "INSERT INTO suppressed_people (hash, reason) VALUES ($1, $2) ON CONFLICT (hash) "
                        "DO UPDATE SET reports = suppressed_people.reports + 1, updated_at = now()",
                        delivery["leads"][idx]["hash"], label)

    async def suppressed_hashes(self) -> frozenset:
        pool = await self._p()
        return frozenset(r["hash"] for r in await pool.fetch("SELECT hash FROM suppressed_people"))

    async def summary(self) -> dict:
        pool = await self._p()
        row = await pool.fetchrow("SELECT (SELECT count(*) FROM feedback_deliveries) d, count(*) n, avg(rating) a "
                                  "FROM feedback")
        labels = defaultdict(int)
        for r in await pool.fetch("SELECT labels FROM feedback"):
            for label in json.loads(r["labels"]).values():
                labels[label] += 1
        supp = await pool.fetchval("SELECT count(*) FROM suppressed_people")
        return {"deliveries": row["d"], "responses": row["n"],
                "avg_rating": round(float(row["a"]), 2) if row["a"] is not None else None,
                "labels": dict(labels), "suppressed_people": supp}


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
        return _page("<h1>Thank you</h1><p>Your feedback was saved. Leads you marked as gone or wrong will not be "
                     "delivered again.</p>")

    @app.get("/feedback/summary", include_in_schema=False)
    async def feedback_summary(request: Request):
        if not admin_token or not hmac.compare_digest(request.headers.get("x-admin-token", ""), admin_token):
            raise HTTPException(status_code=403, detail="forbidden")
        return await get_store().summary()
