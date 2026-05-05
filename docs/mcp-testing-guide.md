# MCP Testing Guide

A complete walkthrough of all 26 `pzk_` tools in logical execution order. Run sections in sequence — later sections reference IDs created in earlier ones.

Replace `{AREA_ID}`, `{PROJECT_ID}`, etc. with the actual IDs returned from each creation call.

---

## Section 1 — PARA Hierarchy Setup

### `pzk_create_area`

Creates an area note (top of the PARA hierarchy).

**Call:**

```json
{
  "title": "Personal Projects",
  "content": "Personal software and creative projects.",
  "cadence": "weekly review"
}
```

**Expected output:**

```
Area created successfully with ID: {AREA_ID}
```

---

### `pzk_get_area`

Returns an area with cadence, linked projects, and per-project task counts.

**Call:**

```json
{
  "area_id": "{AREA_ID}"
}
```

**Expected output:**

```
ID: {AREA_ID}
Cadence: weekly review
Projects: 1

# Test Area

Area for MCP testing.

## Links
- has_part [[{PROJECT_ID}]]

## Projects
- Test Project (ID: {PROJECT_ID}) — 1 task(s)
```

---

### `pzk_list_areas`

Lists all area notes.

**Call:**

```json
{
  "limit": 20
}
```

**Expected output:**

```
Areas (1):

1. Personal Projects (ID: {AREA_ID})
   Cadence: weekly review

```

---

### `pzk_create_project`

Creates a top-level project linked to an area, or a subproject linked to a parent project.

**Call:**

```json
{
  "title": "Parazettel MCP",
  "content": "Build and ship the parazettel fork with PARA/GTD support.",
  "source": "transcript",
  "area_id": "{AREA_ID}",
  "outcome": "Working MCP server with full GTD workflow support",
  "deadline": "2026-04-30"
}
```

To create a subproject through the same tool, pass `parent_project_id` instead of a top-level `area_id`. The project will inherit the parent project's `area_id` automatically.

```json
{
  "title": "Project note retrieval",
  "content": "Add project-scoped context retrieval.",
  "source": "transcript",
  "parent_project_id": "{PARENT_PROJECT_ID}",
  "outcome": "Dedicated project context tool"
}
```

**Expected output:**

```
Project created successfully with ID: {PROJECT_ID}
```

---

### `pzk_create_subproject`

Creates a subproject under an existing parent project. This is the clearer user-facing path when you already know the parent project.

**Call:**

```json
{
  "parent_project_id": "{PARENT_PROJECT_ID}",
  "title": "Project note retrieval",
  "content": "Add project-scoped context retrieval.",
  "source": "transcript",
  "outcome": "Dedicated project context tool"
}
```

**Expected output:**

```
Subproject created successfully with ID: {SUBPROJECT_ID}
```

---

### `pzk_list_projects`

Lists active projects sorted by deadline.

**Call:**

```json
{
  "include_done": false,
  "limit": 20
}
```

**Expected output:**

```
Projects (1):

1. Parazettel MCP (ID: {PROJECT_ID})
   Deadline: 2026-04-30
   Outcome: Working MCP server with full GTD workflow support

```

---

### `pzk_get_project`

Returns a project with task status summary, next-task preview, parent-project context, direct subprojects, and routed notes.

**Call:**

```json
{
  "project_id": "{PROJECT_ID}"
}
```

**Expected output:**

```
ID: {PROJECT_ID}
Area ID: {AREA_ID}
Outcome: Working MCP server with full GTD workflow support
Tasks: 0 total

Next Tasks:
- None

Parent Project:
- Parent initiative (ID: {PARENT_PROJECT_ID})

Subprojects:
- None

Notes:
- None

# Parazettel MCP

Build and ship the parazettel fork with PARA/GTD support.
```

---

## Section 2 — Task Management

### `pzk_create_task`

Creates a task with the full set of optional parameters.

**Call:**

```json
{
  "title": "Write integration tests",
  "content": "Cover the full area → project → task → today view flow.",
  "project_id": "{PROJECT_ID}",
  "status": "ready",
  "due_date": "2026-03-27",
  "priority": 3,
  "energy_level": "high",
  "context": "computer",
  "estimated_minutes": 90,
  "remind_at": "2026-03-26"
}
```

**Expected output:**

```
Task created successfully: Write integration tests (ID: {TASK_ID})
```

**Verify tags were auto-applied** using `pzk_get_note {TASK_ID}` — the note should have `@computer` and `high-energy` in its Tags line.

---

### `pzk_get_note`

Retrieves a note by ID or title. Title appears once (in content), not duplicated in the header.

**Call:**

```json
{
  "identifier": "{TASK_ID}"
}
```

**Expected output:**

```
ID: {TASK_ID}
Type: task
Created: 2026-03-26T...
Updated: 2026-03-26T...
Project ID: {PROJECT_ID}
Area ID: {AREA_ID}
Tags: @computer, high-energy

# Write integration tests

Cover the full area → project → task → today view flow.

## Links
- reference [[{AREA_ID}]]
- part_of [[{PROJECT_ID}]]
```

---

### `pzk_get_notes`

Retrieves multiple notes by ID or title in one call. This is the batch companion to `pzk_get_note` when an agent needs several note bodies as working context.

**Call:**

```json
{
  "identifiers": ["{TASK_ID}", "{PROJECT_ID}"]
}
```

**Expected output:**

```
Notes retrieved (2/2):

ID: {TASK_ID}
Type: task
Created: 2026-03-26T...
Updated: 2026-03-26T...
Project ID: {PROJECT_ID}
Area ID: {AREA_ID}

# Write integration tests

Cover the full area → project → task → today view flow.

---

ID: {PROJECT_ID}
Type: project
Created: 2026-03-26T...
Updated: 2026-03-26T...
Area ID: {AREA_ID}

# Parazettel MCP

Build and ship the parazettel fork with PARA/GTD support.
```

---

### `pzk_get_notes_by_tag`

Retrieves multiple notes with an exact tag match and returns full note bodies. Use this when you know the tag you want and need more than the preview-style output from `pzk_search_notes`.

**Call:**

```json
{
  "tag": "zettelkasten",
  "limit": 10
}
```

**Expected output:**

```
Notes tagged 'zettelkasten' (1):

ID: {NOTE_ID}
Type: permanent
Created: 2026-03-26T...
Updated: 2026-03-26T...
Project ID: {PROJECT_ID}
Area ID: {AREA_ID}

# Atomic notes are the foundation of Zettelkasten

Each note contains exactly one idea.
```

---

### `pzk_get_tasks`

Queries tasks with optional filters.

**Call (all tasks):**

```json
{
  "limit": 20
}
```

**Expected output:**

```
Found 1 task(s):

1. Write integration tests (ID: {TASK_ID})
   Status: ready  Due: 2026-03-27  Priority: 3

```

**Call (filter by status):**

```json
{
  "status": "ready"
}
```

Returns the same task. Try `status="done"` — returns empty.

---

### `pzk_get_project_tasks`

Returns all tasks for a specific project. Use this when the `pzk_get_project` preview is not enough.

**Call:**

```json
{
  "project_id": "{PROJECT_ID}"
}
```

**Expected output:**

```
Tasks for project {PROJECT_ID} (1):

1. Write integration tests (ID: {TASK_ID})
   Status: ready  Due: 2026-03-27

```

---

### `pzk_get_project_notes`

Returns the full note context for non-task notes routed to a specific project. Use this after `pzk_get_project` when you need the actual note bodies, not just note titles. Subprojects are excluded from this output.

**Call:**

```json
{
  "project_id": "{PROJECT_ID}"
}
```

**Expected output:**

```
Project notes for {PROJECT_ID} (1):

ID: {NOTE_ID}
Type: permanent
Created: 2026-04-23T09:00:00
Updated: 2026-04-23T09:00:00
Project ID: {PROJECT_ID}
Area ID: {AREA_ID}

# Project Reference

Useful project context.
```

---

### `pzk_get_todays_tasks`

Returns tasks due today and overdue. Create a second task due today to verify sorting:

**Setup — create an overdue task:**

```json
{
  "title": "Overdue task",
  "content": "Past due.",
  "project_id": "{PROJECT_ID}",
  "due_date": "2026-03-25",
  "priority": 1,
  "status": "active"
}
```

**Call:**

```json
{
  "include_overdue": true
}
```

**Expected output** (sorted by priority descending):

```
Today's tasks (1):

1. Overdue task — due 2026-03-25 (ID: {OVERDUE_TASK_ID})
   Status: active
```

Note: `Write integration tests` is due tomorrow so it does not appear.

---

### `pzk_get_reminders`

Returns notes with `remind_at` on or before today.

**Call:**

```json
{
  "limit": 20
}
```

**Expected output** (the task created with `remind_at: "2026-03-26"` should appear if today ≥ 2026-03-26):

```
Reminders due (1):

1. Write integration tests (ID: {TASK_ID})
   Type: task  Remind: 2026-03-26

```

---

### `pzk_update_task`

Update any mutable field on an existing task — project assignment, due date, priority, estimated minutes, status, remind\_at, recurrence\_rule, or tags.

`pzk_update_task` is the only task update tool. Use it for both ordinary field edits and status transitions.

**Call (set a due date and priority):**

```json
{
  "task_id": "{TASK_ID}",
  "due_date": "2026-04-01",
  "priority": 3,
  "estimated_minutes": 180
}
```

**Expected output:**

```
Task {TASK_ID} updated successfully.
```

**Call (replace tags):**

```json
{
  "task_id": "{TASK_ID}",
  "tags": "review, weekly"
}
```

Passing `tags` replaces the task's existing tags with the provided list.

**Call (reassign to another project):**

```json
{
  "task_id": "{TASK_ID}",
  "parent_project_id": "{NEW_PROJECT_ID}"
}
```

Reassigning the task also updates its `area_id` to match the new project. The old `part_of` / `has_part` project links are removed and the new ones are created automatically.

**Call (complete a recurring task after editing other fields):**

```json
{
  "task_id": "{RECURRING_TASK_ID}",
  "status": "done",
  "priority": 2,
  "due_date": "2026-04-04"
}
```

For recurring tasks, non-status edits are applied first, then marking the task `done` spawns the next instance. The spawned task keeps the same title, project, area, recurrence rule, and PARA linkage.

**Call (invalid due date — error path):**

```json
{
  "task_id": "{TASK_ID}",
  "due_date": "not-a-date"
}
```

**Expected output:**

```
Invalid due_date: not-a-date. Use YYYY-MM-DD.
```

**Call (non-task note — error path):**

```json
{
  "task_id": "{KNOWLEDGE_NOTE_ID}"
}
```

**Expected output:**

```
Note {KNOWLEDGE_NOTE_ID} is not a task (type: permanent)
```

Verify the update took effect: call `pzk_get_tasks project_id="{PROJECT_ID}"` and confirm the new due date and priority values appear in the task list.

Verify the task now surfaces in `pzk_get_todays_tasks` if `due_date` was set to today.

---

## Section 3 — Knowledge Note Management

### `pzk_create_note`

Creates knowledge notes of each type. All non-area notes must include either `area_id` or `project_id`. If `project_id` is supplied, the note inherits the project's `area_id`.

**Call (permanent):**

```json
{
  "title": "Atomic notes are the foundation of Zettelkasten",
  "content": "Each note contains exactly one idea. This constraint forces clarity and enables flexible recombination.",
  "note_type": "permanent",
  "tags": "zettelkasten,methodology,atomicity",
  "source": "transcript",
  "area_id": "{AREA_ID}"
}
```

**Expected output:**

```
Note created successfully with ID: {NOTE_ID}
```

Also test `note_type` values: `fleeting`, `literature`, `structure`, `hub`.

For project-scoped knowledge notes, swap `area_id` for `project_id`. The note will inherit the project's area and get the matching structural links.

---

### `pzk_update_note`

Updates an existing note's content, metadata, and project/area routing. A title-only rename rewrites the leading H1 instead of leaving a stale heading behind. If other notes link to the renamed note via wiki-links, their title aliases are refreshed automatically without bumping those source notes' timestamps.

**Call:**

```json
{
  "note_id": "{NOTE_ID}",
  "title": "Atomic notes are the foundation of a durable Zettelkasten",
  "tags": "zettelkasten,methodology,atomicity,core-principle",
  "project_id": "{PROJECT_ID}"
}
```

Use `parent_project_id` when you want to express project routing in parent-project terms:

```json
{
  "note_id": "{NOTE_ID}",
  "parent_project_id": "{PARENT_PROJECT_ID}"
}
```

**Expected output:**

```
Note updated successfully: {NOTE_ID}
```

---

### `pzk_create_link` — unidirectional

**Call:**

```json
{
  "source_id": "{NOTE_ID}",
  "target_id": "{PROJECT_ID}",
  "link_type": "supports",
  "description": "Atomic design principle applies to task decomposition"
}
```

**Expected output:**

```
Link created from {NOTE_ID} to {PROJECT_ID}
```

---

### `pzk_create_link` — bidirectional with part\_of

**Call:**

```json
{
  "source_id": "{TASK_ID}",
  "target_id": "{NOTE_ID}",
  "link_type": "reference",
  "bidirectional": true
}
```

**Expected output:**

```
Bidirectional link created between {TASK_ID} and {NOTE_ID}
```

---

### `pzk_remove_link`

**Call:**

```json
{
  "source_id": "{TASK_ID}",
  "target_id": "{NOTE_ID}",
  "bidirectional": true
}
```

**Expected output:**

```
Bidirectional link removed between {TASK_ID} and {NOTE_ID}
```

---

### `pzk_delete_note`

**Setup** — create a throwaway note:

```json
{
  "title": "Temporary note",
  "content": "This will be deleted.",
  "note_type": "fleeting",
  "source": "transcript",
  "area_id": "{AREA_ID}"
}
```

**Call:**

```json
{
  "note_id": "{TEMP_NOTE_ID}"
}
```

**Expected output:**

```
Note deleted successfully: {TEMP_NOTE_ID}
```

Verify deletion: `pzk_get_note {TEMP_NOTE_ID}` should return `Note not found: {TEMP_NOTE_ID}`.

**Link cleanup verification** — if any other note had an outgoing link to the deleted note, that link is automatically removed from its markdown file on deletion. Verify by calling `pzk_get_note` on any source note that linked to it — the reference should be gone.

---

## Section 4 — Discovery and Search

### `pzk_search_notes`

**Call (by text query):**

```json
{
  "query": "atomic",
  "limit": 10
}
```

**Expected output:**

```
Found 1 matching notes:

1. Atomic notes are the foundation of Zettelkasten (ID: {NOTE_ID})
   Tags: zettelkasten, methodology, atomicity, core-principle
   Created: 2026-03-26
   Preview: Each note contains exactly one idea. This constraint forces clarity...

```

**Call (by note\_type):**

```json
{
  "note_type": "task",
  "limit": 10
}
```

Returns only task-type notes.

**Call (by tags):**

```json
{
  "tags": "zettelkasten"
}
```

Returns notes tagged with `zettelkasten`.
Use `pzk_get_notes_by_tag` instead when you want full note bodies rather than search previews.

**Call (combined filters):**

```json
{
  "query": "python",
  "tags": "python,javascript",
  "note_type": "task",
  "status": "ready",
  "project_id": "{PROJECT_ID}",
  "area_id": "{AREA_ID}",
  "limit": 10
}
```

All provided filters are combined with `AND`, except `tags`, which still match when any supplied tag is present.

**Call (invalid status — error path):**

```json
{
  "status": "flying"
}
```

**Expected output:**

```
Invalid status: flying. Valid: inbox, ready, scheduled, active, waiting, someday, done, cancelled, archived, evergreen
```

---

### `pzk_get_linked_notes`

**Call:**

```json
{
  "note_id": "{NOTE_ID}",
  "direction": "both"
}
```

**Expected output:**

```
Found N both linked notes for {NOTE_ID}:

1. Parazettel MCP (ID: {PROJECT_ID})
   Link type: supports
   Description: Atomic design principle applies to task decomposition

```

---

### `pzk_get_all_tags`

**Call:** *(no parameters)*

**Expected output:**

```
Found N tags:

1. @computer
2. high-energy
3. atomicity
4. core-principle
5. methodology
6. zettelkasten
...
```

Tags are sorted alphabetically.

---

### `pzk_find_similar_notes`

**Call:**

```json
{
  "note_id": "{NOTE_ID}",
  "threshold": 0.1,
  "limit": 5
}
```

**Expected output:**

```
Found N similar notes for {NOTE_ID}:

1. Some Related Note (ID: ...)
   Similarity: 0.35
   Tags: zettelkasten
   Preview: ...

```

Returns empty if no other notes share tags or links.

---

### `pzk_find_central_notes`

**Call:**

```json
{
  "limit": 10
}
```

**Expected output:**

```
Central notes in the Zettelkasten (most connected):

1. Parazettel MCP (ID: {PROJECT_ID})
   Connections: 3
   Tags: ...
   Preview: Build and ship the parazettel fork...

```

---

### `pzk_find_orphaned_notes`

Notes with no incoming or outgoing links.

**Setup** — create an area note without linking it to anything:

```json
{
  "title": "Orphaned test area",
  "content": "This area intentionally has no links.",
  "note_type": "area"
}
```

**Call:** *(no parameters)*

**Expected output:**

```
Found 1 orphaned notes:

1. Orphaned test area (ID: {ORPHAN_ID})
   Preview: # Orphaned test area  This area intentionally has no links.

```

> **Note:** Every non-area note in the test run is auto-linked by PARA routing. An area note is the simplest way to create an intentionally orphaned note for this check.

---

### `pzk_list_notes_by_date`

**Call (single day):**

```json
{
  "start_date": "2026-03-26",
  "end_date": "2026-03-26",
  "limit": 5
}
```

**Expected output:**

```
Notes created between 2026-03-26 and 2026-03-26 (showing N results):

1. Orphaned test note (ID: {ORPHAN_ID})
   Created: 2026-03-26 19:05
   Preview: # Orphaned test note  This note has no links...

```

**Call (multi-day range):**

```json
{
  "start_date": "2026-03-01",
  "end_date": "2026-03-31",
  "limit": 10
}
```

Returns all notes created during March 2026. Use a range that spans your actual note creation dates — if the vault is new, a single-day range that matches your test session is sufficient.

**Call (recently updated — use `use_updated=true`):**

```json
{
  "use_updated": true,
  "start_date": "2026-03-26",
  "limit": 10
}
```

Returns notes *updated* on or after the start date, sorted by `updated_at`. Useful for finding recently revised notes regardless of when they were created.

---

## Section 5 — Maintenance

### `pzk_rebuild_index`

Rebuilds the SQLite index from the Markdown files on disk. Use after manually editing `.md` files. A timestamped `.bak` backup of the SQLite database is created before the rebuild starts.

**Call:** *(no parameters)*

**Expected output:**

```
Database index rebuilt successfully.
Notes processed: N
Change in note count: 0
```

`Change in note count` will be non-zero only if `.md` files were added or removed outside the MCP server.

---

## Quick Reference

| Tool | Required params | Key optional params |
| --- | --- | --- |
| `pzk_create_area` | title, content | cadence, tags |
| `pzk_get_area` | area\_id | — |
| `pzk_list_areas` | — | limit |
| `pzk_create_project` | title, content, source, area\_id or parent\_project\_id | outcome, deadline, tags |
| `pzk_create_subproject` | parent\_project\_id, title, content, source | outcome, deadline, tags |
| `pzk_list_projects` | — | include\_done, limit |
| `pzk_get_project` | project\_id | — |
| `pzk_get_project_notes` | project\_id | limit |
| `pzk_get_project_tasks` | project\_id | status, limit |
| `pzk_create_task` | title, content, project\_id | status, due\_date, priority, energy\_level, context, remind\_at, recurrence\_rule |
| `pzk_update_task` | task\_id | project\_id, parent\_project\_id, due\_date, priority, status, remind\_at, estimated\_minutes, recurrence\_rule, tags |
| `pzk_get_tasks` | — | status, project\_id, due\_date, overdue\_only, priority, limit |
| `pzk_get_todays_tasks` | — | include\_overdue |
| `pzk_get_reminders` | — | limit |
| `pzk_create_note` | title, content | note\_type, source, area\_id, project\_id, tags |
| `pzk_get_note` | identifier | — |
| `pzk_update_note` | note\_id | title, content, note\_type, tags, status, project\_id, parent\_project\_id, area\_id |
| `pzk_delete_note` | note\_id | — |
| `pzk_create_link` | source\_id, target\_id | link\_type, description, bidirectional |
| `pzk_remove_link` | source\_id, target\_id | bidirectional |
| `pzk_search_notes` | — | query, tags, note\_type, status, project\_id, area\_id, limit |
| `pzk_get_linked_notes` | note\_id | direction |
| `pzk_get_all_tags` | — | — |
| `pzk_find_similar_notes` | note\_id | threshold, limit |
| `pzk_find_central_notes` | — | limit |
| `pzk_find_orphaned_notes` | — | — |
| `pzk_list_notes_by_date` | — | start\_date, end\_date, use\_updated, limit |
| `pzk_rebuild_index` | — | — |
