# API result recovery implementation plan

**Goal:** Recover already submitted asynchronous media results after a worker is lost, without resubmitting generation or resuming downstream nodes.

**Design:** Capture successful, recognized submission responses at the existing HTTP audit boundary. Store task identity and encrypted query credentials in a private scheduler table. A cross-process file lock serializes recovery. Only worker_lost jobs are eligible. Download to deterministic owner-scoped paths using temporary files and atomic rename; merge results idempotently into persistent history. Keep the interrupted workflow failed and label recovered outputs explicitly.

**Approaches:** Re-running a workflow risks duplicate charges. Replaying audit logs cannot authenticate because secrets are redacted. Selected approach is submission checkpoints plus explicit read-only provider query adapters. The user approved this recovery scope in the preceding conversation and requested implementation.

**Tech:** Python, SQLite, existing filelock and safe media downloader; cryptography Fernet for credentials, derived from the existing internal secret with domain separation.

**Scope:** Ark contents/generations/tasks, Wan OPC and Vapeur, Kling official video endpoints and Vapeur submit/describe. Signed Tencent/LAS, synchronous responses, and old submissions without checkpoints are outside automatic recovery. No changes outside Account Manager; no remote restart/deployment.

## Tasks
- [x] Add explicit provider recognition/query/result parsers, with fixture tests.
- [x] Add durable recovery store/worker and audit observer with encrypted credentials, tests for restart, duplicate processing, retry, isolation, failed provider, and local completion.
- [x] Wire startup and persistent history/assets, preserving failed workflow status and existing outputs.
- [x] Run focused and complete suites, review the patch, document supported protocols and operational limits.

Validation: 129 Python tests, 6 frontend unit tests, and the browser refresh suite
passed against the installed ComfyUI frontend, including WebSocket disconnect/reconnect.
Provider responses/downloads were simulated; no live paid generation, deployment,
or production restart was performed.

## Review focus
- Never submit a second generation request; only known GET paths or Vapeur describe POST.
- Credentials never enter ordinary audit logs, history, manifests, or error messages.
- Multiple workers and crashes must not duplicate history entries or expose another user's outputs.
- Provider timeout/authentication failure must not become a completed workflow.
- Live jobs and cancelled jobs must not be recovered; unknown protocols must not be guessed.
