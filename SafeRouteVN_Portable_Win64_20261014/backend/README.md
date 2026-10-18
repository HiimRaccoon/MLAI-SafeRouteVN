# SafeRoute VN — Member 3 backend

FastAPI app 0.8.0: pinned catalog/load/read, asynchronous optimize/compute, certified accept, observed event/replay, durable SDK notification inbox, verified startup recovery and immutable public audit exports. Includes durable three-profile comparisons, explicit discrete autoplay/speed/pause, Vietnamese factual narrative and a separately launched read-only mock app.

For the current installation use `backend/scripts/run_backend.py --local-root <new-private-installation> --offline`. See [completion API handoff](../docs/M3_COMPLETION_API_HANDOFF.md) and [operations runbook](../docs/M3_OPERATIONS_RUNBOOK.md). The commands below use the earlier Step 7 private installation.

Run from the project root using the installed backend environment:

```powershell
& 'D:\MLAI4\.local\m3-step7\backend-venv\Scripts\python.exe' -B backend/scripts/start_backend.py
# Separate terminal:
& 'D:\MLAI4\.local\m3-step7\backend-venv\Scripts\python.exe' -B backend/scripts/start_worker.py
```

HTTP defaults to `http://127.0.0.1:8000`; `/docs` and `/openapi.json` describe the HTTP contract. `/health` is liveness; `/ready` requires verified source/build, auth, metadata and a fresh READY worker. First startup validates every existing READY session before reconciling pending commands and dispatching jobs. Worker and API use private installation/auth/metadata files outside the project. Never place bearer tokens or authority SQLite in frontend assets.

Public SDK access runs in an isolated, byte-verified subprocess. M3 owns HTTP/session/request/queue/audit metadata; the pinned M2 SDK owns physical authority, witness, replay and outbox. Browser input cannot select server paths, source, build, budget or raw state. SDK integrity failures remain evidence and block writes.

Ordinary SDK calls are bounded to 60 seconds; inspection/notification/admin calls to 120 seconds. Recovery inspection batches at most two sessions per SDK subprocess, with a 120-second bound per session and separate recovery/validation/resolve/notification checks for each session. Startup synchronizes every validated session before opening write gates; periodic notification polling fairly rotates one session per turn, with ten seconds between completed turns. This is eventual coverage, not a per-session refresh SLA. Worker-owned mutation tasks remain protected until SDK and durable M3 receipts finish, even during repeated cancellation. Timeout logs report operation, access wait and remaining subprocess budget without request fields, SDK output, credentials or server paths.

Non-compute SDK calls share a cross-process access lock keyed by resolved authority location, including calls from API and worker processes with different M3 metadata databases. Waiting and subprocess execution share the original deadline; lock exhaustion returns `RUNTIME_BUSY` without launching SDK or marking corruption. Compute remains concurrent for public polling/cancellation, so this lock does not exclude every internal SDK transaction. SDK integrity rejection still blocks writes and preserves evidence.

The fourth native trial returned `STORE_INVALID` during a foreground state read. Lock contention is an inference supported by the pinned SDK's SQLite error mapping, successful isolated S2/S3/S4 lineage validation, and a genuine concurrent notification-batch/HTTP-state probe after coordination was added. The original HTTP diagnostic does not directly prove the SQLite cause. Authority was preserved without repair or reset.

Tests and native acceptance are separate:

```powershell
& 'D:\MLAI4\.local\m3-step7\backend-venv\Scripts\python.exe' -B -m pytest backend/tests -q -p no:cacheprovider
& 'D:\MLAI4\.local\m3-step7\backend-venv\Scripts\python.exe' -B backend/scripts/verify_step7.py --output '<new-private-directory>/native_audit.json'
```

The native audit verifies prior witnessed S2/S3/S4 receipts and live heads, tests actual persist/ack commit boundaries by repository reopen and stable-key retry, public exports, restart, private backup and isolated corruption rejection. These boundary checks are not process fault injection. It performs no new solve. Stop API and worker before the private backup command `backend/scripts/backup_backend.py --api-stopped`; read its coordinated admin procedure first.

M3-08 operations revision adds a sealed offline wheel kit, fresh installer, coordinated localhost launcher and opt-in Python socket policy, including the actual M2 native compute child. See [operations runbook](../docs/M3_OPERATIONS_RUNBOOK.md), [11-criterion matrix](../docs/M3_STEP8_CRITERIA_MATRIX.md), and [M4 E2E checklist](../docs/M3_M4_E2E_CHECKLIST.md) for receipt status and scope. The socket policy is an audited rehearsal mechanism, not an OS security sandbox.

See [HTTP handoff](../docs/M3_API_HANDOFF.md), [outbox/recovery/artifact procedure](../docs/M3_OUTBOX_RECOVERY_ARTIFACT_API_HANDOFF.md), and [implementation plan](../docs/M3_BACKEND_IMPLEMENTATION_PLAN_20261005.md). Replay is SIMULATED_REPLAY, safety is relative exposure PROXY, runtime performance is NOT_MET. Profile comparison, controlled autoplay, narrative and the isolated mock app are implemented in 0.8.0. Actual frontend E2E remains pending M4 source.
