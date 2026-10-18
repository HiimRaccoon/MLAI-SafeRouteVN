# Read-only MOCK_DEMO for Member 4

This separate localhost app supplies the byte-pinned S2/S3/S4 execution examples for adapter, map and KPI development. It requires explicit opt-in and its own bearer token. It never creates sessions, jobs, authority stores or physical state. A timeout or failure in the native app never switches to this demo.

From the team project root, with the installed backend Python:

```powershell
& $BackendPython -m backend.mock.serve --enable-read-only-demo --token-file D:\private\mock_token.txt --port 8001
```

The token file must be absolute and outside the shared project. Put a separately generated random token of 32 to 256 ASCII letters, digits, `_` or `-` in it. Send `Authorization: Bearer <mock-token>` for reads. Do not reuse native auth credentials or commit the token file. The launcher always binds `127.0.0.1`; it does not accept a host, installation, source or authority argument. Stop it with Ctrl+C.

Routes:

| GET route | Result |
| --- | --- |
| `/health` | Explicit `MOCK_DEMO` liveness |
| `/api/mock/capabilities` | Read-only flags, units and validation scope |
| `/api/mock/scenarios` | Exactly S2/S3/S4, fixture SHA and historical build |
| `/api/mock/scenarios/{id}/execution-view` | Labeled wrapper containing the unchanged parsed execution view |
| `/api/mock/scenarios/{id}/source` | Exact frozen JSON bytes and `X-Fixture-SHA256` |

Mutation methods return `405 / MOCK_READ_ONLY`. Query fields and GET bodies are rejected. Only explicit frontend origins are accepted by CORS. Every response has `X-SafeRoute-Mode: MOCK_DEMO`; the source endpoint keeps exact bytes rather than adding fields to the frozen view. Render a visible demo label in the UI.

The source package is build `80694f51...`; these historical example executions retain build `41c792f9...` in their basis. They are examples from that package, not fresh jobs on the installed build. The service verifies exact manifest/file hashes and public execution shape; it does not attest raw SQLite/VRP validity, native solves, optimality, live observations or browser E2E. Metric scopes, exact integer strings, rational values, nulls and exposure PROXY remain unchanged. Wire distances use m, forecasts s, observed/action durations us, mass kg and money VND.

Switch M4 to the native HTTP handoff for session/load/compute/accept/events/replay. Mock route identifiers and `rolling-S2` example session IDs are not real native authority handles.
