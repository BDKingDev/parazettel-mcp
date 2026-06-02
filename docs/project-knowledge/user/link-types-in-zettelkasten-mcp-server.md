================================================================
Start of Project Knowledge File
================================================================

Purpose:
--------
This file is designed to be consumed by AI systems for analysis, review,
or other automated processes. It solely serves the purpose of background
information and should NOT under any circumstances leak into the user's
interaction with the AI when actually USING the Parazettel MCP tools to
process, explore or synthesize user-supplied information.

Content:
--------

Link Types in Parazettel MCP
-----------------------------

Parazettel uses a semantic linking system for knowledge relationships and a structural linking system for PARA/GTD hierarchy. Links are directional and most have semantic inverses.

Semantic Link Types (knowledge graph)
--------------------------------------

| Primary | Inverse | Relationship Description |
| --- | --- | --- |
| `reference` | `reference` | Simple reference to related information (symmetric) |
| `extends` | `extended_by` | One note builds upon or develops concepts from another |
| `refines` | `refined_by` | One note clarifies or improves upon another |
| `contradicts` | `contradicted_by` | One note presents opposing views to another |
| `questions` | `questioned_by` | One note poses questions about another |
| `supports` | `supported_by` | One note provides evidence for another |
| `related` | `related` | Generic thematic connection (symmetric) |

Structural Link Types (PARA/GTD)
----------------------------------

| Primary | Inverse | Relationship Description |
| --- | --- | --- |
| `part_of` | `has_part` | This task or note belongs to a project or area |
| `blocks` | `blocked_by` | This task blocks another task from starting |

Structural links are created automatically by `pzk_create_task` (PART\_OF to project) and `pzk_create_project` (PART\_OF to area). They can also be created manually with `pzk_create_link`.

Using Link Types
-----------------

```text
pzk_create_link source_id=NOTE_A target_id=NOTE_B link_type=supports
```

Any value from either the Primary or Inverse column is valid as `link_type`.

Wiki-link Aliases
-----------------

Parazettel accepts Obsidian-style piped wiki-links such as `[[NOTE_ID|Displayed Title]]`. On ingest, only the note ID portion is used for indexing, so aliases and heading fragments do not break link resolution.

When a note is rewritten, touched wiki-links are normalized to `[[NOTE_ID|Current Note Title]]` when the title is safe for wiki-link syntax. Renaming a note refreshes stale incoming title aliases, and deleting a note removes incoming markdown references.

Bidirectional Links
--------------------

Set `bidirectional=true` to automatically create the semantic inverse in the reverse direction:

```text
pzk_create_link source_id=NOTE_A target_id=NOTE_B link_type=supports bidirectional=true
```

This creates:

1. A `supports` link from NOTE\_A → NOTE\_B
2. A `supported_by` link from NOTE\_B → NOTE\_A

For symmetric types (`reference`, `related`), both directions use the same link type.

Bidirectional inverses are automatic
------------------------------------

When you pass `bidirectional=true`, the reverse link's type is chosen automatically from the forward type — you do not (and cannot) specify it. For example, `supports` ⇄ `supported_by`, `extends` ⇄ `extended_by`, `part_of` ⇄ `has_part`; symmetric types (`reference`, `related`) use the same type both ways.

```text
pzk_create_link source_id=NOTE_A target_id=NOTE_B link_type=supports bidirectional=true
# creates supports A→B and supported_by B→A
```

The `pzk_create_link` parameters are `source_id`, `target_id`, `link_type`, `description`, and `bidirectional`. There is no parameter to override the inverse type.

Best Practices
---------------

- **Choose specific types**: Use `supports`, `extends`, or `contradicts` rather than falling back to `reference`
- **Direction matters**: The source note is the one making the claim about the relationship
- **Bidirectional for important relationships**: Use `bidirectional=true` for conceptual relationships that should be navigable both ways
- **Structural links are auto-managed**: `part_of`/`has_part` links are created by the PARA tools — avoid duplicating them manually
- **Build knowledge paths**: Sequential `extends` or `supports` links that develop an argument over multiple notes
- **Balance link types**: A healthy vault uses supportive, contradictory, and questioning links — not just references

Link Type Meanings in Detail
------------------------------

- **reference/reference**: Simple connection — no specific relationship implied
- **extends/extended_by**: Source builds upon and develops the target's concepts
- **refines/refined_by**: Source makes the target's concepts more precise or corrects them
- **contradicts/contradicted_by**: Source challenges or opposes the target's claims
- **questions/questioned_by**: Source raises uncertainty or inquiry about the target
- **supports/supported_by**: Source provides evidence or backing for the target's claims
- **related/related**: Thematic connection when no more specific type applies
- **part_of/has_part**: PARA structural relationship — task or note belongs to a project or area
- **blocks/blocked_by**: GTD dependency — source must complete before target can start

================================================================
End of Project Knowledge File
================================================================
