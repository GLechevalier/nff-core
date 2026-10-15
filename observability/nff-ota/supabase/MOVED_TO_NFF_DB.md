# ⚠️ These migrations moved to `nff-db/`

`supabase/migrations/0001…0006_*.sql` are now part of the single linear sequence in
`nff-db/migrations/` (they became `0003_ota_initial` … `0008_ota_pipeline_verified`), applied by
`nff-db` — see `nff-db/README.md`.

**Do not apply the files in this directory directly.** They are kept only until `nff-db`'s
`db:check` confirms zero drift on the live dev database, then delete this `supabase/` folder
(see `DEPLOY_ROADMAP.md §2`).
