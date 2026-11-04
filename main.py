"""Entry point: MIP-003 API server (masumi SDK) for Crypto BD Lead Finder.

    python main.py                 # API mode, needs the Masumi Payment Service (see README)
    python main.py --standalone    # run one job locally without payments
"""

import argparse
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv

# Tests set BDLF_NO_DOTENV=1 so real keys from .env never enter a test run.
if os.getenv("BDLF_NO_DOTENV") != "1":
    load_dotenv()

from masumi import Config  # noqa: E402

from app.agent import input_schema, process_job  # noqa: E402
from app import feedback, ledger, masumi_compat  # noqa: E402
from app.job_store import PostgresJobStorage, recover_jobs  # noqa: E402
from app.errors import install_error_handling  # noqa: E402
from app.logs import setup_logging  # noqa: E402
from app.report import FOOTER  # noqa: E402
from app.job_gate import StartJobGate, add_job_gate  # noqa: E402
from app.server import Health, ReliableAgentServer, add_admin_routes  # noqa: E402
from app.settings import Settings  # noqa: E402

EXPIRE_INTERVAL_S = 600

masumi_compat.install()

setup_logging()

DEMO = {
    "input": {"company": "Example Labs", "role": "partnerships"},
    "output": {
        "result": (
            "# BD leads: partnerships at Example Labs\n\n## 1. Jane Doe\n- **Title:** Head of Partnerships\n"
            "- **Company:** Example Labs\n- **Profile:** https://www.linkedin.com/in/janedoe-example\n"
            "- **Source:** treg.to\n\n> Hi Jane, ...\n\n---\n" + FOOTER
        )
    },
}


def remove_api_docs(app) -> None:
    """Take out FastAPI's documentation routes (BL-17). The SDK creates the app, and FastAPI registers them in its
    constructor, so they are removed from the router; the URLs are cleared so nothing re-adds or links them."""
    docs_paths = {app.openapi_url, app.docs_url, app.redoc_url, app.swagger_ui_oauth2_redirect_url} - {None}
    app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", None) not in docs_paths]
    app.openapi_url = app.docs_url = app.redoc_url = app.swagger_ui_oauth2_redirect_url = None


def create_app(job_storage=None, feedback_store=None, job_ledger=None, health=None):
    """Build the MIP-003 app plus the feedback pages. With AGENT_DATABASE_URL set, jobs and feedback
    survive restarts."""
    settings = Settings.from_env()
    config = Config(
        payment_service_url=os.getenv("PAYMENT_SERVICE_URL", ""),
        payment_api_key=os.getenv("PAYMENT_API_KEY", ""),
    )
    dsn = os.getenv("AGENT_DATABASE_URL")
    if job_storage is None and dsn:
        job_storage = PostgresJobStorage(dsn, table="agent_jobs_pro" if settings.is_pro else "agent_jobs")
    if feedback_store is None:
        feedback_store = feedback.PostgresFeedbackStore(dsn) if dsn else feedback.MemoryFeedbackStore()
    feedback.set_store(feedback_store)
    if job_ledger is None:
        job_ledger = ledger.PostgresLedger(dsn) if dsn else ledger.MemoryLedger()
    ledger.set_ledger(job_ledger)
    health = health or Health(os.getenv("PAYMENT_SERVICE_URL", ""))
    name = "Crypto BD Lead Finder Pro" if settings.is_pro else "Crypto BD Lead Finder"
    blocked = settings.sale_blocked_reason()
    gate: Optional[StartJobGate] = None  # set once the server exists

    async def availability() -> dict:
        # The registry hides agents that report unavailable, so buyers cannot pay while we are degraded,
        # paused or out of today's data budget (BL-33).
        problem = await gate.availability_problem() if gate else blocked
        if problem:
            return {"status": "unavailable", "type": "masumi-agent", "message": f"{name} is paused: {problem}"}
        status, detail = await health.check()
        message = f"{name} is operational" if status == "available" else f"{name} is degraded: {detail}"
        return {"status": status, "type": "masumi-agent", "message": message}

    server = ReliableAgentServer(
        config=config,
        agent_identifier=os.getenv("AGENT_IDENTIFIER"),
        network=os.getenv("NETWORK", "Preprod"),
        job_storage=job_storage,
        start_job_handler=process_job,
        input_schema_handler=input_schema(settings.tier, settings.enrich_people_per_job),
        availability_handler=availability,
        demo_handler=lambda: DEMO,
        seller_vkey=os.getenv("SELLER_VKEY"),
        tier=settings.tier,
    )
    app = server.get_app()
    admin_token = os.getenv("ADMIN_TOKEN") or os.getenv("FEEDBACK_ADMIN_TOKEN", "")
    feedback.add_feedback_routes(app, settings.feedback_secret, admin_token)
    gate = StartJobGate(settings, blocked, server)
    add_admin_routes(app, server, settings, admin_token, gate)
    add_job_gate(app, gate)
    install_error_handling(app)  # last: its identifier check must run before the gate
    if not settings.api_docs_enabled:
        remove_api_docs(app)
    if not settings.feedback_secret:
        logging.getLogger(__name__).warning("FEEDBACK_SECRET not set: reports carry no feedback link")

    if isinstance(job_storage, PostgresJobStorage):
        # Wrap the SDK's lifespan: connect and recover jobs before serving, close the pools after.
        inner = app.router.lifespan_context

        async def expire_loop():
            # Unsettled attempts past their counts_until become crashed cost rows (idempotent, both agents).
            while True:
                try:
                    await job_ledger.expire_attempts()
                except Exception as e:
                    logging.getLogger(__name__).warning("Attempt expiry failed: %s", type(e).__name__)
                await asyncio.sleep(EXPIRE_INTERVAL_S)

        @asynccontextmanager
        async def lifespan(a):
            await job_storage.connect()
            await recover_jobs(server)
            expiry = asyncio.create_task(expire_loop())
            try:
                async with inner(a):
                    yield
            finally:
                expiry.cancel()
                await job_storage.close()
                for closable in (feedback_store, job_ledger):
                    if hasattr(closable, "close"):
                        await closable.close()

        app.router.lifespan_context = lifespan
    else:
        logging.getLogger(__name__).warning("AGENT_DATABASE_URL not set: jobs are kept in memory only")
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--standalone", action="store_true", help="run one job without the payment flow")
    parser.add_argument("--input", default='{"company": "Chainlink Labs", "role": "partnerships"}',
                        help="job input as JSON (standalone mode)")
    args = parser.parse_args()

    if os.getenv("NETWORK", "Preprod") != "Preprod":
        raise SystemExit("This build is for Preprod only. Set NETWORK=Preprod.")

    if args.standalone:
        print(asyncio.run(process_job("standalone", json.loads(args.input))))
        return

    import uvicorn

    uvicorn.run(create_app(), host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8080")))


if __name__ == "__main__":
    main()
