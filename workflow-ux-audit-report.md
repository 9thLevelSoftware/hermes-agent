# Workflows UX/UI E2E Audit Report

Date: 2026-07-08
Workspace: /tmp/hermes-workflow-audit
Target UI: Hermes dashboard Workflows plugin at http://127.0.0.1:9135/workflows?audit=final
Audit scope: blank-slate workflow creation, adding cells/triggers, editing supported cell types, switching cell types, connecting cells, validating, deploying, and running a workflow through the dashboard UI without editing JSON/YAML.

## Executive summary

Result: PASS for the audited happy path after fixes.

The workflow builder had several UI-only dead ends: a user could inspect or edit existing workflow JSON/YAML, but could not reliably start from a blank workflow, add new cells, switch cell types, add switch cases, connect cells, validate/deploy without opening Advanced YAML, or safely redeploy a same-version workflow. These blockers have been addressed with dashboard controls and backend deploy retry behavior.

Live browser evidence confirms a blank workflow was created entirely through workflow-builder controls, two cells were added and edited, cell types were switched, the draft was validated/deployed from the Draft Review panel, and the deployed workflow was run successfully. The resulting execution completed with both node runs succeeded.

## Audit method

1. Mapped workflow surfaces in the dashboard bundle and plugin API.
2. Ran parallel frontend/backend audit passes to identify UI dead ends and API failures.
3. Added failing coverage for UI-only helper flows and deploy edge cases.
4. Implemented minimal UX fixes in the existing dashboard bundle and backend deploy endpoint.
5. Re-ran targeted test gates.
6. Restarted an isolated dashboard on port 9135.
7. Drove a live browser from a blank workflow using dashboard fields/buttons/controls only; no JSON/YAML editing was used in the UI audit.

## Fixed blockers and dead ends

### 1. Blank slate creation had no full UI path

Severity: Critical
Area: Workflow creation

Before: users could describe a workflow or open Advanced YAML, but the visual builder did not provide a complete blank-slate creation path.

Fix: added UI builder controls to start from a blank workflow with a workflow name, maintain an active draft spec, and immediately show the draft in Draft Review / Visual Workflow Editor.

Files changed:
- plugins/workflows/dashboard/dist/index.js
- tests/plugins/test_workflows_dashboard_assets.py
- tests/plugins/test_workflows_dashboard_plugin.py

### 2. Adding cells required JSON/YAML knowledge or unreliable dropdown behavior

Severity: Critical
Area: Cell creation

Before: users could not reliably add all supported cell types via visible controls. The browser audit also exposed that dropdown-only type selection was brittle in automation and easy to miss in the UI.

Fix: added visible Add workflow cell controls with New cell id, Cell type input/select, insert-after source, and one-click shortcut buttons for pass, switch, agent_task, wait, parallel, join, and fail.

Evidence from live browser:
- Added `review` as `agent_task` from the blank workflow.
- Added `summary` as `pass` after `review`.
- Draft Review reflected `review -> summary`.

### 3. Switching cell types was not discoverable or complete

Severity: Critical
Area: Cell editing

Before: existing cells had type-specific editors, but there was no plain UI path to switch a selected cell between supported types.

Fix: added visible `Switch selected cell to: ...` controls and made the selected cell editor route through the correct basic/agent editor based on the current type state.

Evidence from live browser:
- Switched `summary` from `pass` to `wait`, confirmed in Draft Review.
- Switched `summary` back to `pass`.
- Switched `review` from `agent_task` to `pass`, confirmed profile/provider-only fields were no longer shown as agent config in Draft Review.

### 4. Switch cells could not be fully edited through UI controls

Severity: High
Area: Switch/case editing

Before: switch cell cases/default branch handling required advanced JSON/YAML or stale node state.

Fix: added switch default input plus dynamic Add switch case controls, and persisted switch cases from dedicated UI state instead of relying on stale node contents.

Automated evidence:
- `test_dashboard_ui_builder_helpers_add_schedule_trigger_and_switch_branch_edges`
- `test_dashboard_ui_builder_helpers_create_add_connect_delete_cells_without_json`

### 5. Triggers and edges were not fully wired in the visual builder

Severity: High
Area: Graph construction

Before: triggers and edges existed in the spec model, but the visual builder did not expose reliable UI-only controls for adding triggers or connecting cells, and edge canonicalization did not consistently handle triggers/cells.

Fix: added Add trigger controls for manual/schedule triggers, Connect cells controls, and canonicalized edge upsert logic around cellRef/triggerRef references.

Automated evidence:
- Schedule trigger helper coverage.
- Edge helper coverage.

Live browser note:
- The final happy-path run used the default manual trigger and `review -> summary` connection because that is the simplest executable UI-only proof path.

### 6. Draft validation/deploy forced users back into Advanced YAML

Severity: Critical
Area: Deploy flow

Before: Validate and Deploy buttons only lived in the Advanced YAML panel, so a user who built visually had to open YAML to finish.

Fix: added `Validate draft` and `Deploy draft` buttons directly to Draft Review. They reuse the same validation/deploy backend calls while keeping the user in the plain review flow.

Evidence from live browser:
- `Validate draft` returned `Validated ux_audit_final`.
- `Deploy draft` returned `Deployed ux_audit_final`.
- Workflow list displayed `UX Audit Final enabled ux_audit_final · v1`.

### 7. Redeploying same workflow/version could fail instead of returning the existing definition

Severity: High
Area: Backend deploy API

Before: deploying the same workflow/version could raise a version-exists error, creating a dead end for iterative visual edits and redeploys.

Fix: the Workflows dashboard deploy endpoint now catches VersionExistsError and returns the existing definition record instead of surfacing a hard failure.

Automated evidence:
- `test_dashboard_deploy_same_workflow_returns_existing_version`
- `test_dashboard_deploy_auto_bumps_changed_same_version_specs`

### 8. Empty run/timeline states lacked enough confirmation

Severity: Medium
Area: Execution feedback

Before: some node run/execution states showed sparse or dead-end copy.

Fix: node run rendering now includes explicit fallback copy, and the live run path confirms execution list, node runs, events, and terminal status are visible.

Evidence from live browser:
- Execution list showed `wfexec_658d75c6b2dd4169 succeeded`.
- Execution detail timeline showed `execution_started`, `node_succeeded` for `review`, `node_succeeded` for `summary`, and `execution_succeeded`.

## Live browser E2E transcript

Environment:
- Isolated dashboard server: port 9135
- URL: http://127.0.0.1:9135/workflows?audit=final
- Browser title: Hermes Agent - Dashboard

Steps performed:
1. Opened Workflows dashboard from a clean isolated HERMES_HOME.
2. Entered workflow name `UX Audit Final` in the visual builder.
3. Clicked Start from blank workflow.
4. Added cell `review` with type `agent_task` using the Add workflow cell controls.
5. Added cell `summary` with type `pass`, inserted after `review`.
6. Verified Draft Review contained:
   - `review`, type `agent_task`, next `summary`
   - `summary`, type `pass`
7. Selected `summary`, switched it to `wait`, and verified Draft Review showed `summary` as `wait`.
8. Switched `summary` back to `pass`.
9. Selected `review`, switched it from `agent_task` to `pass`, and verified Draft Review no longer showed it as an agent task.
10. Clicked Validate draft; observed `Validated ux_audit_final`.
11. Clicked Deploy draft; observed `Deployed ux_audit_final`.
12. Verified workflow list showed `UX Audit Final enabled ux_audit_final · v1`.
13. Clicked Run workflow.
14. Observed execution `wfexec_658d75c6b2dd4169` with status `succeeded`.
15. Observed node runs `review succeeded` and `summary succeeded` plus `execution_succeeded` in the timeline.
16. Checked browser console after the flow: 0 console messages and 0 JavaScript errors.

## Automated verification evidence

Fresh command run after the final Draft Review validate/deploy patch:

```bash
node --check plugins/workflows/dashboard/dist/index.js
/Users/christopherwilloughby/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/plugins/test_workflows_dashboard_assets.py \
  tests/plugins/test_workflows_dashboard_plugin.py::test_dashboard_ui_builder_helpers_create_add_connect_delete_cells_without_json \
  tests/plugins/test_workflows_dashboard_plugin.py::test_dashboard_ui_builder_helpers_add_schedule_trigger_and_switch_branch_edges \
  tests/plugins/test_workflows_dashboard_plugin.py::test_dashboard_cleaned_node_removes_agent_only_fields_when_switching_types \
  tests/plugins/test_workflows_dashboard_plugin.py::test_dashboard_deploy_auto_bumps_changed_same_version_specs \
  -q
```

Result: `12 passed in 1.28s`.

Live browser result:
- `Validated ux_audit_final`
- `Deployed ux_audit_final`
- `wfexec_658d75c6b2dd4169 succeeded`
- Node runs: `review succeeded`, `summary succeeded`
- Browser console: `total_messages=0`, `total_errors=0`

## Remaining risks / follow-ups

1. React Flow drag/drop edge creation was not exhaustively verified manually. The builder now has a non-canvas Connect cells form and automated edge tests, so this is not a blocker for UI-only workflow creation.
2. Agent-task live execution was not performed with a real provider call in the final browser path. The final run intentionally switched both cells to `pass` to prove deploy/run deterministically without consuming model credentials. Agent-task routing remains covered by prior routing tests and dashboard Draft Review behavior.
3. The dashboard bundle is edited directly in `plugins/workflows/dashboard/dist/index.js`; if a source-generation path is later introduced, these UX fixes should be moved to source and regenerated.
4. The browser automation used dashboard controls and form entries only, but for a few button activations it used DOM-click automation because the browser snapshot layer truncated lower-page controls. No workflow JSON/YAML or backend database records were edited directly.

## Files changed

- plugins/workflows/dashboard/dist/index.js
- plugins/workflows/dashboard/plugin_api.py
- tests/plugins/test_workflows_dashboard_assets.py
- tests/plugins/test_workflows_dashboard_plugin.py

## Final audit conclusion

The workflow UX no longer dead-ends on the audited path. A user can start from a blank workflow, add cells, switch between supported cell types, validate/deploy from Draft Review without opening Advanced YAML, and run the deployed workflow from the dashboard. The remaining risks are non-blocking coverage gaps around canvas drag/drop and real provider-backed agent-task execution.