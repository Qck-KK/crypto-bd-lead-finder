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

from dotenv import load_dotenv

# Tests set BDLF_NO_DOTENV=1 so real keys from .env never enter a test run.
if os.getenv("BDLF_NO_DOTENV") != "1":
    load_dotenv()

from masumi import Config, MasumiAgentServer  # noqa: E402

from app.agent import input_schema, process_job  # noqa: E402
from app import feedback, masumi_compat  # noqa: E402
from app.job_store import PostgresJobStorage, recover_jobs  # noqa: E402
from app.report import FOOTER  # noqa: E402
from app.settings import Settings  # noqa: E402

masumi_compat.install()

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

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


async def availability() -> dict:
    name = "Crypto BD Lead Finder Pro" if Settings.from_env().is_pro else "Crypto BD Lead Finder"
    return {"status": "available", "type": "masumi-agent", "message": f"{name} is operational"}


def create_app(job_storage=None, feedback_store=None):
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

    server = MasumiAgentServer(
        config=config,
        agent_identifier=os.getenv("AGENT_IDENTIFIER"),
        network=os.getenv("NETWORK", "Preprod"),
        job_storage=job_storage,
        start_job_handler=process_job,
        input_schema_handler=input_schema(settings.tier),
        availability_handler=availability,
        demo_handler=lambda: DEMO,
        seller_vkey=os.getenv("SELLER_VKEY"),
    )
    app = server.get_app()
    feedback.add_feedback_routes(app, settings.feedback_secret, os.getenv("FEEDBACK_ADMIN_TOKEN", ""))
    if not settings.feedback_secret:
        logging.getLogger(__name__).warning("FEEDBACK_SECRET not set: reports carry no feedback link")

    if isinstance(job_storage, PostgresJobStorage):
        # Wrap the SDK's lifespan: connect and recover jobs before serving, close the pools after.
        inner = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(a):
            await job_storage.connect()
            await recover_jobs(server)
            try:
                async with inner(a):
                    yield
            finally:
                await job_storage.close()
                if hasattr(feedback_store, "close"):
                    await feedback_store.close()

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
