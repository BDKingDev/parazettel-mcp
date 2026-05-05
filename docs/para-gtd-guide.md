# PARA/GTD Guide

Parazettel extends Zettelkasten with a GTD-inspired action layer using the PARA organisational structure. This guide explains the concepts, the hierarchy, and how to work with tasks, projects, and areas day-to-day.

---

## Concepts

### PARA

PARA is an organisational system built on four categories:

- **Projects** — active outcomes with a specific end state and deadline
- **Areas** — ongoing responsibilities with no end date (health, finances, a team you manage)
- **Resources** — reference material you may want later (handled as knowledge notes in Parazettel)
- **Archive** — inactive items, marked with `status=archived`

### GTD (Getting Things Done)

GTD is a workflow for capturing, clarifying, and acting on commitments:

- **Capture** everything into the inbox — don't decide yet
- **Clarify** each inbox item: is it actionable? What's the next action?
- **Organise** into projects, areas, someday lists, and waiting lists
- **Reflect** weekly: review open loops, update statuses
- **Engage** using the today view and context filters

### How they fit together in Parazettel

Tasks, projects, and areas are note types in the same graph as your knowledge notes. They share IDs, tags, and semantic links. A project note can link to the research notes that inform it; a task can reference the book note that prompted it.

---

## The Hierarchy

```
Area  (ongoing responsibility — no end date)
 └── Project  (active outcome with deadline)
       └── Task  (single next action)
```

- **Areas** are hub-like notes. They serve as entry points to a domain.
- **Projects** are structure-like notes. They organise tasks and context around a single outcome.
- **Tasks** must belong to a project. Their `area_id` is auto-filled from the project.
- **Knowledge notes** should also be routed into the hierarchy. Non-area notes need either an explicit `area_id` or a `project_id`, and project-scoped notes inherit the project's `area_id`.

---

## Setting Up Your Hierarchy

### 1. Create an area

```text
pzk_create_area
  title: "Health"
  content: "Physical and mental wellbeing."
  cadence: "weekly review"
```

### 2. Create a project linked to the area

```text
pzk_create_project
  title: "Run a 5K"
  content: "Train for and complete a 5K race."
  source: "transcript"
  area_id: <area_id>
  outcome: "Complete a 5K race in under 30 minutes"
  deadline: "2026-06-01"
```

For subprojects, use `pzk_create_subproject` when you want the hierarchy to be explicit:

```text
pzk_create_subproject
  parent_project_id: <project_id>
  title: "Three-run weekly cadence"
  content: "Create a focused subproject for the weekly training schedule."
  source: "transcript"
  outcome: "A repeatable weekly 5K training plan"
```

Advanced callers can also use `pzk_create_project` with `parent_project_id: <project_id>`. In both cases, the subproject inherits the parent project's `area_id` automatically.

### 3. Create tasks linked to the project

```text
pzk_create_task
  title: "Research beginner running plan"
  content: "Find a 12-week Couch to 5K plan."
  project_id: <project_id>
  due_date: "2026-03-30"
  priority: 2
  energy_level: "medium"
  context: "computer"
```

The task's `area_id` is automatically set from the project — you don't need to specify it.

### 4. Create knowledge notes routed to the area or project

```text
pzk_create_note
  title: "A 5K plan is easier to sustain when runs are pre-scheduled"
  content: "Planning runs on the calendar reduces decision friction and improves follow-through."
  note_type: "permanent"
  source: "transcript"
  project_id: <project_id>
```

Non-area notes must include either `area_id` or `project_id`. If you provide `project_id`, the note inherits the project's `area_id` automatically.

Use `pzk_get_project` when you want a quick project summary with task counts, next tasks, parent-project context, direct subprojects, and routed note titles. Use `pzk_get_project_notes` when you want the full bodies of all non-task, non-project notes routed to the project as working context.

---

## Status Lifecycle

### For tasks and action items

```
inbox → ready → active → done
              ↓
           waiting (blocked on someone/something)
           scheduled (committed to specific date)
           someday (not ready to commit)
           cancelled
```

| Status | When to use |
| --- | --- |
| `inbox` | Just captured — needs triage |
| `ready` | Triaged, clear next action, available to start |
| `scheduled` | Has a specific date you've committed to |
| `active` | You're working on it now |
| `waiting` | You're blocked — delegated or awaiting input |
| `someday` | Good idea, but not committing now |
| `done` | Completed |
| `cancelled` | No longer relevant |

### For knowledge notes

| Status | When to use |
| --- | --- |
| `evergreen` | Mature note that rarely needs updating |
| `archived` | No longer actively referenced |

---

## Energy Levels and Contexts

### Energy levels

Use `energy_level` on `pzk_create_task` to tag tasks by cognitive demand. This lets you match tasks to your current capacity.

| Parameter value | Tag applied | Use for |
| --- | --- | --- |
| `high` | `high-energy` | Deep focus work, writing, complex problems |
| `medium` | `mid-energy` | Meetings, emails, routine decisions |
| `low` | `low-energy` | Admin, filing, simple physical tasks |

### Contexts

Use `context` on `pzk_create_task` to tag tasks by where or how you can do them. This filters your task list to what's possible right now.

```text
pzk_create_task ... context="computer"  → applies tag @computer
pzk_create_task ... context="home"      → applies tag @home
pzk_create_task ... context="phone"     → applies tag @phone
pzk_create_task ... context="outside"   → applies tag @outside
```

You can then search for tasks by context:

```text
pzk_search_notes note_type="task" tags="@computer" status="ready"
```

---

## Today View

`pzk_get_todays_tasks` returns all tasks that are:

- Due today or earlier (overdue)
- Not in `done`, `cancelled`, `archived`, or `evergreen` status
- Sorted by priority descending, then due date ascending

This is your daily starting point. Work from top to bottom.

```text
pzk_get_todays_tasks include_overdue=true
```

Example output:

```
Today's tasks (3):

1. [P3] Prepare sprint review slides — due 2026-03-26 (ID: ...)
   Status: ready

2. [P2] Review PR from Alex — due 2026-03-26 (ID: ...)
   Status: active

3. [P1] Expense report — due 2026-03-25 (ID: ...)
   Status: ready
```

---

## Reminders

Any note type — not just tasks — can have a `remind_at` date. When that date arrives, the note surfaces in `pzk_get_reminders`.

Use reminders for:

- A fleeting note you want to revisit in a week
- A waiting task you want to follow up on
- A literature note due for review

```text
pzk_create_task
  title: "Follow up with Jordan on proposal"
  project_id: <id>
  status: "waiting"
  remind_at: "2026-04-01"
```

```text
pzk_get_reminders limit=20
```

---

## Recurring Tasks

Set `recurrence_rule` on a task to have the next instance spawn automatically when you mark it done.

| Rule | Interval |
| --- | --- |
| `daily` | Every day |
| `weekly` | Every 7 days |
| `monthly` | Every 30 days |
| `quarterly` | Every 91 days |
| `yearly` | Every 365 days |

```text
pzk_create_task
  title: "Weekly review"
  project_id: <id>
  due_date: "2026-03-28"
  recurrence_rule: "weekly"
  status: "ready"
```

When you complete it:

```text
pzk_update_task task_id=<id> status="done"
```

A new task is created with `due_date = 2026-04-04`, `status=ready`, and the same project linkage. If you reassign the task to a different project before marking it done, the spawned task keeps that new project and its inherited `area_id`. The completed task is kept as a record.

---

## Weekly Review

Run this each week to keep your system clean:

1. **Inbox** — `pzk_get_tasks status="inbox"` — triage everything
2. **Today/overdue** — `pzk_get_todays_tasks` — resolve anything past due
3. **Active projects** — `pzk_list_projects` — check for stalled projects
4. **Waiting** — `pzk_get_tasks status="waiting"` — follow up on delegated items
5. **Reminders** — `pzk_get_reminders` — action any due reminders
6. **Orphaned notes** — `pzk_find_orphaned_notes` — link or discard
7. **Fleeting notes** — `pzk_search_notes note_type="fleeting"` — process or delete

---

## PARA Conventions

- **One area per domain of life** — keep the list short (5–10 areas)
- **Projects always under an area** — orphaned projects are a warning sign
- **Projects have clear outcomes** — "Launch feature X by Q2" not "Work on feature X"
- **Tasks are single next actions** — "Draft email to Jordan" not "Handle Jordan situation"
- **Archive completed projects** — `pzk_update_note note_id=<id> status="archived"`; tasks within them are preserved
