# Pause one current batch

- Add only batch_paused (default false) to accepted trigger receipts. Supply manual ALTER; do not execute production DDL.
- Manager/source-scoped PUT toggles the field. Serialize with parent definition and receipt locks, matching existing retry lock order.
- Before new dispatch claims, lock and check the owning batch. Previously issued callbacks/accepted execution and result recovery may finish.
- Automatic/manual/daily business retry admission checks the same locked flag. Keep queued work and attempt counts intact.
- Return batch_paused on board list/detail; add pause/resume to the existing detail header and a list tag. Do not change job definitions, timers, future batches or business status.
- Test pause, resume, in-flight recovery, source/role rejection and all retry entry points.
