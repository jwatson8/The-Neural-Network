# CI/CD: test automatically, deploy when ready

**CI** checks that a code change works. **CD** takes a checked version and installs it on the VM. Our workflow is [integration.yml](../.github/workflows/integration.yml).

1. **Tests and coverage:** every push and pull request runs all `test_*.py` tests under `src`, including database tests. Python network sockets are disabled, so accidental Kafka/API/LLM calls fail. Tests use local SQLite, mocks and recorded course fixtures. The existing model integration test also fits the checked-in M0 snapshot; this is not production retraining.
2. **Docker verification:** after tests pass, build the image, train the recorded fixture, start an isolated Compose deployment (including PostgreSQL initialization), and check health and recommendation responses. No live services or production credentials are used. Temporary CI volumes are deleted afterward, never production volumes.
3. **Deploy:** only a manual run on `main` with `deploy` checked can deploy, and both preceding jobs must pass in that same run. A runner inside the campus network connects over SSH. The VM refuses a stale commit or tracked local edits, builds the selected commit, restarts Compose and checks recommendations. It keeps the existing model, `.env`, and database/profile volumes. Concurrent deployments are serialized.

## View results or run tests yourself

Open the repository **Actions → CI and deployment → a run**. Each job has logs. Download `test-reports` for `tests.xml` (JUnit), `coverage.xml`, `coverage.md`, and `html/index.html` (open locally). `docker-reports` contains container status and logs. Coverage describes measured execution, not proof that every behavior is correct; currently untested modules remain visible in the report. There is no arbitrary coverage threshold yet.

```bash
python -m pip install -r src/requirements-test.txt
python -m pytest
```

Old M1 comparison/plotting scripts in `planning/` are historical experiments, not the current production test suite. Add new infrastructure tests anywhere under `src` using `test_*.py` names. Tests must replace external boundaries with fixtures/mocks.

## One-time deployment setup (team administrator)

The VM has a private campus address. GitHub-hosted runners cannot be assumed to reach it. Install a **Linux x64 self-hosted runner** on a trusted machine that can SSH to the VM (the VM itself can be used for this small class project). In repository **Settings → Actions → Runners → New self-hosted runner**, follow GitHub's current download/registration commands and add the label **`cmu-deploy`**. Run it as a service using the provided service instructions. Keep runner installation outside `/home/the-neural-network`, for example under a dedicated user's home. Do not paste the temporary registration token into chat or commit it.

This runner executes trusted `main` deployment code only in this workflow; PR tests use hosted runners. Repository writers can change workflows, so everyone with write access must be trusted. Prefer a separate deployment machine if available. [GitHub's runner security guidance](https://docs.github.com/en/actions/reference/security/secure-use).

The SSH deployment account needs access to `/home/the-neural-network`, permission to use Docker without an interactive sudo password, and permission to fetch the GitHub repository. Docker access is privileged: use a trusted team account. Verify these while logged into that account:

```bash
cd /home/the-neural-network
docker version
docker compose version
git -c safe.directory=/home/the-neural-network fetch origin main
test -f .env && test -s artifacts/model.joblib
```

Keep production settings in the VM's `.env`. The newer Compose setup requires `POSTGRES_PASSWORD`; the live profile also requires the course Kafka/user API settings and OpenAI key. Do not print `.env` into workflow logs. The deployment account must be able to read it.

Create a dedicated SSH key for deployment, authorize its **public** key for the VM account, and configure a `production` environment under **Settings → Environments**. Restrict deployment branches to `main` and add a required reviewer if your GitHub plan supports it. Add these environment secrets:

| Secret | Value |
|---|---|
| `DEPLOY_HOST` | `CMU-ONLINE-MLIP-VM01.ANDREW.CMU.EDU` |
| `DEPLOY_USER` | The authorized VM deployment account, e.g. `jacew` |
| `DEPLOY_SSH_KEY` | Dedicated SSH private key (noninteractive, stored only as a secret) |
| `DEPLOY_KNOWN_HOSTS` | Verified SSH known-hosts entry for the exact hostname above |

Obtain the host key from a trusted existing SSH setup, or collect it with `ssh-keyscan` and compare its fingerprint to `/etc/ssh/ssh_host_ed25519_key.pub` over an already trusted VM session. Do not blindly trust a new scanned key. SSH host-key checking stays enabled.

## Deploy a release

Merge the reviewed PR into `main`. Open **Actions → CI and deployment → Run workflow**, choose **main**, check **deploy**, and leave **live** checked when the production Kafka/LLM settings are ready. The workflow first reruns both checks, then deploys. A queued deployment usually means the `cmu-deploy` runner is offline or missing; cancel it if you are not ready. Unchecking `live` does not stop already-running live containers; it only omits starting/updating them.

The server checkout is detached at the exact tested commit intentionally. Future deployments fetch and select a new exact commit, rather than silently following a moving branch. Do not make source edits in that server checkout.

After success, confirm the public `/recommend/1` endpoint from the campus network and inspect the course Kafka results. The workflow's in-container check does not prove external reachability, live LLM generation, or course traffic success. `state/deployed-commit.txt` records the last successful deployment.

Short restart downtime is accepted. A failed update is reported as failed and may require manual recovery; it is not automatically rolled back. The workflow log records the previous checkout SHA before the update. To recover, check out the known-good commit on the VM, set `DEPLOY_TAG` to that commit, and run its documented Compose command. Do not delete volumes. Database schema changes may need their own migration/recovery plan; `create_all` is not a general schema migration tool.

## Milestone 2 scope

This is the infrastructure CI and deliberate code-deployment pipeline. The separate requirement to retrain from **newer production data** and deploy an updated model still needs the team's model-update script; `src.model.prepare` currently enforces the fixed M0 data checksums. Do not claim a real production deployment until the runner is configured and a deployment run passes. Link an actual passing Actions run and its coverage artifact in `m2_report.md`.
