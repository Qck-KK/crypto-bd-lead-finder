# Deploy runbook: production hardening (BL-16, BL-32, BL-17) over `c78580d`

Code only. No new dependencies (`requirements.txt` and `Dockerfile` unchanged since `c78580d`), no environment
change (`API_DOCS_ENABLED` unset keeps the docs off), no database migration. Every step needs the owner's go-ahead.
Postgres, the Payment Service, the backup service and their host folders (`deploy/data/`) are never touched: every
compose command names `agent agent-pro` with `--no-deps --no-build`.

```bash
# Always through the InstaBox CLI: it handles authentication and refreshes an expired certificate.
S="npx --yes instabox@latest ssh finder-agent --"
C=<the approved commit>        # pinned once; used for the archive, DEPLOYED_COMMIT and the checks
R=/data/workspace/crypto-bd-lead-finder
```

## Before (read only)

1. **The commit holds the new files and LF text:**
   ```bash
   git ls-tree -r --name-only $C app | grep -E "app/(errors|identifiers)\.py"   # both listed
   git ls-files --eol app scripts main.py | grep -v "^i/lf"                       # prints nothing
   ```
2. **No job in progress** (`docs/DEPLOY_REVIEW.md`, pre-deploy check 1): no `running`, `awaiting_payment` or
   `awaiting_input` rows in `agent_jobs` / `agent_jobs_pro`.
3. **Rollback image present and running:** `deploy-agent:c78580d`, `deploy-agent:latest` and both agent containers
   show the same image id (`9e02aade…` at review time):
   ```bash
   $S "docker image ls deploy-agent; cd $R/deploy && for s in agent agent-pro; do docker inspect --format '{{.Name}} {{.Image}}' \$(docker compose ps -q \$s); done"
   ```
4. **Payment request count** (to compare after the bad-identifier check):
   ```bash
   $S "cd $R/deploy && docker compose exec -T -e PGOPTIONS='-c default_transaction_read_only=on' postgres psql -X -q -At -U masumi -d payment -c 'SELECT count(*) FROM \"PaymentRequest\"'"
   ```

## Deploy

1. **Backup:** `$S "cd $R/deploy && docker compose exec -T backup sh /deploy/backup.sh"`
2. **Upload exactly `$C`, LF:**
   ```bash
   git -c core.autocrlf=false -c core.eol=lf archive --format=tar $C app scripts main.py | $S "tar -x -C $R && echo $C > $R/DEPLOYED_COMMIT"
   ```
3. **Code-only image, both agents together** (the new image is built on `deploy-agent:latest` = `c78580d`; the
   `deploy-agent:c78580d` tag keeps the old image):
   ```bash
   $S "cd $R && printf 'FROM deploy-agent:latest\nCOPY app ./app\nCOPY scripts ./scripts\nCOPY main.py .\n' > /tmp/Dockerfile.codeonly && DOCKER_BUILDKIT=0 docker build -q -f /tmp/Dockerfile.codeonly -t deploy-agent:latest . && cd deploy && docker compose up -d --no-build --no-deps --force-recreate agent agent-pro"
   ```

## Verify

- **Same commit in both agents:** both containers show the new `deploy-agent:latest` id, and the code hash in each
  container and on disk equals the hash of `git archive $C` (compare per-file `sha256sum` lists; on Windows strip
  the `*` that marks binary mode before hashing the list).
- **BL-17:** `/docs`, `/redoc`, `/openapi.json`, `/docs/oauth2-redirect` answer 404 on 8080 and 8081;
  `/availability`, `/input_schema`, `/demo` unchanged.
- **BL-16:** `POST /start_job` with `"identifier_from_purchaser": "abc"` answers 400, and the payment request count
  from step 4 above is unchanged.
- **BL-32:** the startup log has `Recovered jobs after restart` only if there were unfinished jobs, and no
  `Recovery of job ... failed` line.
- **Preflight** in both containers: standard 0 FAIL; Pro only the intended "Pro enrichment enabled" FAIL.
- **Optional:** paid acceptance on standard (Preprod test tokens, owner's go-ahead).

## Rollback (image and source together; no database step: the schema is unchanged)

1. **Image:** a tag alone does not change running containers; recreate them.
   ```bash
   $S "docker tag deploy-agent:c78580d deploy-agent:latest && cd $R/deploy && docker compose up -d --no-build --no-deps --force-recreate agent agent-pro"
   ```
2. **Source:** `tar -x` never deletes, so first remove the files `$C` added, then restore `c78580d`:
   ```bash
   git diff --name-only --diff-filter=A c78580d $C -- app scripts main.py    # app/errors.py app/identifiers.py
   $S "cd $R && rm -f app/errors.py app/identifiers.py"
   git -c core.autocrlf=false -c core.eol=lf archive --format=tar c78580d app scripts main.py | $S "tar -x -C $R && echo c78580d940088f8f74c0c89354d32fd8fd145605 > $R/DEPLOYED_COMMIT"
   ```
3. **Check:** both containers run `deploy-agent:c78580d`'s id, and the code hash in both containers and on disk is
   the `c78580d` hash again (`d5c942f2…`, 27 files, at review time).
