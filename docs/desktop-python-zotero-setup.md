# Desktop Python environment and Zotero import

## User entry points

- Fresh installations show a Python selection screen before starting the local backend. Choose an installed CPython 3.12+ matching the running app architecture, or explicitly choose managed Python 3.12.
- Settings → Local runtime uses the same component. It supports bounded detection, native executable selection, validation, application-only PATH directories, preparation progress and cancellation.
- Library list → Import Zotero library is available beside New library in both the toolbar and empty state, only when connected to the Desktop local backend.
- Select one Collection, optionally rename the new personal library, then create and sync. Descendant Collections are included. Import resolves local PDF references without copying or parsing PDFs; summaries are not automatically generated.
- Existing bindings can be opened from the import dialog. Creating a second library requires an explicit choice. Inside a library, Zotero sync settings retains manual sync and unlink controls.

## Runtime persistence and safety

The host contract is version 5 and advertises `python.environment.manage`. The renderer calls `host.python.status`, `detect`, `pick`, `validate`, `prepare` and `cancel`. Preparation automatically applies after successful validation and task draining; it is not a backend HTTP operation and works before login.

Configuration is atomically stored in `<userData>/engine/python-environment.json`, with separate current and pending selections. New environments are created at their final `<userData>/engine/runtimes/<uuid>` paths. Existing legacy `<userData>/engine/venv` environments are recognized. Installed dependency identity is separate from packaged backend source identity so source-only changes do not reinstall Python/dependencies.

Local mode sets `UV_PYTHON_DOWNLOADS=never`. Both modes isolate uv state in Polaris data and remove inherited Python/virtualenv overrides. No shell profile or system Python installation is changed. A compatible interpreter is only a candidate: dependency installation and backend checks must still pass.

While switching, `/api/desktop-runtime/drain` establishes a renewable admission barrier. Existing HTTP/streaming requests, inline tasks and Vault reconciliation finish; already-admitted requests and worker continuations can complete their task chains. The old backend remains active during preparation. A failed activation restores the previous runtime when available. Backend migration snapshots retain their existing rollback behavior. Runtime replacement does not move or delete database, papers, PDFs or credentials.

## Import transactions

`POST /api/zotero-local/import-library` validates the local Zotero Collection, then commits the personal library, binding, queued sync run and receipt together. Receipts are unique by `(user_id, request_id)` and store a payload fingerprint. Identical retries return the existing result; conflicting reuse returns 409. Queue dispatch happens after commit; a dispatch failure leaves a recoverable queued run for the existing startup/scheduler recovery.

Migration `615363d9c6af` adds the receipt table after `9a7d4c2e6f10`. Server profile retains the existing import workflow and rejects local Zotero/runtime operations.

## Original Zotero PDFs (0.1.3)

- Import and reconciliation resolve attachment keys and local paths with bounded concurrency, including for unchanged parent metadata. Paths remain in the Desktop database, never API responses. Migration `57022e4415e1` adds these fields; existing bindings backfill them on the next sync.
- Batch item requests paginate using the result count, since Local Zotero can expand requested keys into additional records. Missing keys receive an individual retry; extra records do not enter the requested set. Error samples use copy-on-assignment for JSON persistence.
- Reading automatically uses the authenticated, Desktop-only `GET /libraries/{library_id}/papers/{paper_id}/zotero-local-pdf` endpoint. It resolves the latest Zotero path and streams the original, including Range requests; it never writes a PDF blob. If Zotero is offline, a previously resolved local file can still be read. Deleted/missing originals give an explicit error, without exposing their absolute paths.
- Summary preparation hashes and validates the original, registering an asset and immutable content identity without copying bytes. Parsing consumes the original path. Unchanged bytes reuse content versions; in-place replacement is detected even when Zotero's attachment version does not change. Old evidence cannot silently read newer bytes: digest mismatches require a new content version. Parsed text, indexes and summaries still consume disk space.
- Previously saved PDF copies are not deleted automatically. Zotero originals are never modified. Historical text/revisions remain available, but the old PDF bytes cannot be recovered from Polaris if an uncopied original is overwritten or deleted.

## Verification commands

```sh
cd src/backend
.venv/bin/pytest -q tests/test_desktop_setup.py tests/test_zotero_local.py tests/test_migrations.py
```

From repository root, using Node 22 and the pinned pnpm version:

```sh
pnpm --dir src/desktop run test:python
pnpm --dir src/desktop run e2e:local-integrations
pnpm --dir src/desktop run smoke
make desktop-dist
pnpm --dir src/desktop run e2e:packaged-setup
```

The packaged setup test uses a temporary profile and a random `POLARIS_DESKTOP_ENGINE_PORT`, never the user's existing backend or database. It chooses the repository backend Python as a local interpreter, verifies both buttons and subsequent environment reuse, then switches to managed Python 3.12 through Settings and verifies reconnection. It retains its isolated profile/screenshots for inspection. It does not import the user's real Zotero Collection.

The Mac output is Universal (arm64 + x86_64) with ad-hoc signatures, not Apple Developer ID notarization. Native Intel execution must be tested on Intel hardware separately from architecture inspection.

## 0.1.1 verification

- 66 targeted backend regressions passed, including migration roundtrip and concurrent import receipts; Alembic has one head.
- 217 frontend tests, 105 kernel tests and 10 Python runtime tests passed; desktop/frontend TypeScript and changed-backend Ruff checks passed.
- Desktop IPC smoke and real-browser integration checks passed, including narrow-screen import, explicit Collection selection, retry request identity and cancellation.
- Packaged arm64 execution passed first-run local Python selection, dependency installation, session establishment, both import entry points, second-startup reuse, and switching to managed Python 3.12 with reconnection.
- The App passed deep/strict signature verification after the complete runtime workflow. Dependency builds use a writable source copy rather than modifying sealed App resources.
- ZIP integrity and DMG checksums passed. Main executable, Electron framework and bundled uv contain x86_64 and arm64 slices. Intel hardware execution and Apple notarization were not performed.

SHA-256:

```text
08d3f68f7f405c95ca66a38911a58fcbe7cc19e710083de3821f330c73b971c4  Polaris-0.1.1-universal-mac.zip
9ed7b21a11458294d956f8609d0914681f735e423a76895c8c0d25be5b3f75c8  Polaris-0.1.1-universal.dmg
```
