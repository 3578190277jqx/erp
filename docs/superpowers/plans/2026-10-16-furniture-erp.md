# Furniture ERP Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement task-by-task in the current session. User requested direct execution and GitHub commit visibility.

**Goal:** Deliver a runnable Chinese furniture ERP with real storage, permissions, workflow and Apple-inspired UI.

**Architecture:** Django monolith. Typed relational business models, explicit atomic services, scoped module views, shared server-rendered templates and local static assets. SQLite for local review, optional PostgreSQL through environment configuration.

**Tech Stack:** Python 3.12+, Django 5.2.18, WhiteNoise 6.12.0, Waitress 3.0.2, Tabler core 1.6.1 CSS, native JavaScript.

**Spec:** `docs/superpowers/specs/2026-10-16-furniture-erp-design.md`

## Global Constraints

- Chinese UI; CNY with Decimal and two decimal places; no float amount calculations.
- Owner, organization and role scopes enforced on server, including FK choices, exports and reports.
- All mutations use POST, CSRF, atomic workflow service, optimistic versions and audit events.
- Self-host all UI assets; localStorage contains display preferences only.
- Preserve initial Git history. Work on `feat/furniture-erp`; push incremental commits as requested.
- No native mobile builds, legal general ledger, electronic signature, automated nesting or external payments claims.

## Review Focus

- Cross-organization IDs in fields, batch actions, exports and foreign-key selectors must not leak data.
- Concurrent or repeated stock receipts, issues, quote conversion and payments must not duplicate effects.
- Empty, negative, non-finite and excessive money/quantity inputs must fail before any write.
- Readonly or approved documents must not be editable through alternate routes or nested lines.
- Production settings, seed credentials and fresh-install instructions must match the actual application.

## File structure

`config/` settings and URLs; `erp/models.py` relational data; `erp/permissions.py` role and scope policy; `erp/services/` workflow, stock and finance transactions; `erp/registry.py` declarative module UI; `erp/forms.py` validated scoped forms; `erp/views/` auth, dashboard and module views; `erp/templates/` common UI and module screens; `erp/static/` local Tabler, app CSS and JS; `erp/tests/` domain and HTTP regression tests; `erp/management/commands/seed_demo.py` idempotent demo setup; `scripts/` setup and asset vendoring; `docs/` design and validation; `.github/workflows/ci.yml` CI.

### Task 1: Persistent business model and permission foundation

**Files:** `config/*`, `manage.py`, `requirements.txt`, `erp/models.py`, `erp/permissions.py`, `erp/tests/test_domain.py`, initial migrations.

**Interfaces:** Produces `scoped(user, model, organization=None) -> QuerySet`, `can(user, module, action, organization=None) -> bool`, `Organization`, `Membership`, `Customer`, `Measurement`, `Design`, `Quote`, `QuoteLine`, `Order`, `Breakdown`, `BOMLine`, `Material`, `Warehouse`, `Stock`, `StockMovement`, `Production`, `Purchase`, `PurchaseLine`, `Delivery`, `Installation`, `AfterSales`, `Payment`, `ChangeRequest`, `AuditEvent`.

- [ ] Write tests for organization tree scope, own-only scope, no-membership denial and non-finite/negative decimal constraints. Run tests and observe missing implementation.
- [ ] Implement models, constraints and permissions; migrate; run domain tests.
- [ ] Commit and push `feat: add ERP domain models and organization permissions`.

### Task 2: Atomic business lifecycle

**Files:** `erp/services/*`, `erp/tests/test_workflows.py`, `erp/tests/test_stock_finance.py`.

**Interfaces:** Consumes Task 1 models and permissions. Produces `transition(user, obj, action, version, note='')`, `add_line(...)`, `adjust_stock(...)`, `record_payment(...)`, `approve_change(...)`. Services enforce permissions independent of views.

- [ ] Write and run failing lifecycle tests: quotation total/approval/conversion, unique conversion, freeze approved lines, release BOM, production planning/issue/start/complete, delivery/install prerequisites, change maker/checker.
- [ ] Write and run failing transaction tests: shortage rolls back all rows, stock adjustment reason and nonnegative quantities, repeat receipt/issue, overpayment, duplicate reference, stale version, negative/non-finite amount.
- [ ] Implement explicit transactions, compare-and-swap locking, legal transitions, unique constraints and audit snapshots. Run all tests.
- [ ] Commit and push `feat: implement furniture order production inventory and finance workflows`.

### Task 3: Usable Apple-inspired business interface

**Files:** `erp/registry.py`, `erp/forms.py`, `erp/views/*`, templates, app CSS/JS, vendored Tabler CSS, `erp/tests/test_views.py`.

**Interfaces:** Consumes scoped querysets and services; produces authenticated list/detail/create/edit/line/action/batch/export routes for registered modules, organization switching, dashboards and reports.

- [ ] Write and run failing HTTP tests: auth redirect, CSRF, scoped details and list/export, forbidden relation IDs, approved edit denial, bulk rollback, valid form save, dashboard rendering and theme controls.
- [ ] Implement shared navigation, filters, pagination, read views, forms, version tokens, modal confirmation, batch operations and CSV export.
- [ ] Apply system typography, #0071e3 accent, glass toolbar, folding sidebar, density, reduced motion and light/dark themes. Financial/chart summaries use actual scoped queries.
- [ ] Verify all tests; browser check at 1440px/390px, light/dark, create and workflow actions; capture screenshots.
- [ ] Commit and push `feat: add responsive ERP workspace and all business module screens`.

### Task 4: Reproducible startup, demo data, CI and delivery

**Files:** `seed_demo.py`, `scripts/*`, Dockerfile, `compose.yml`, environment example, README, `docs/VALIDATION.md`, `.github/workflows/ci.yml`, seed tests.

**Interfaces:** Consumes all module/service interfaces. Produces documented fresh-install commands, random demo password, CI test/check pipeline and optional production container.

- [ ] Write and run failing seed idempotency tests. Implement two isolated demo organizations, role accounts and coherent sample documents without known production credentials.
- [ ] Verify clean SQLite install, seed twice, all tests, migration drift, collectstatic and Django deploy checks with secure environment settings.
- [ ] Request a fresh branch review per executing-plans; fix important findings with regression tests.
- [ ] Update validation evidence and completion statuses; commit and push. Open a reviewable draft PR and attach it to the chat if useful; keep main intact unless user asks to merge.
