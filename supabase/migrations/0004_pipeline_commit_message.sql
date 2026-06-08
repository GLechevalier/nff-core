-- nff-github: carry the commit message through the pipeline
-- =============================================================================
-- A pipeline_run is the firmware analogue of a Vercel deployment, and like Vercel
-- it should be shown to humans by its COMMIT MESSAGE ("test push deploy"), not by
-- the machine-derived firmware version ("test-nff-afdc45f"). The connector captures
-- the push's head-commit message and stores it here; the dashboard's Deploys list and
-- the nff-ota deployment name both render it.
--
-- Pre-req: 0003_github.sql (pipeline_runs) must already be applied.
-- Safe to re-run: guarded with IF NOT EXISTS.
-- =============================================================================

alter table pipeline_runs add column if not exists commit_message text;
