"""Reading documents into the candidate graph, deterministically.

The pipeline, and where each part lives:

===========================  ==========================================================
:mod:`lines`                 extracted text as numbered lines; the links block
:mod:`sections`              which heading a line sits under
:mod:`header`                name, contact details, links — and the dropped address
:mod:`dates`                 month ranges, and refusing to invent a missing month
:mod:`metrics`               numbers that are results, and not versions or years
:mod:`skills`                skills from a list, and from prose, via the taxonomy
:mod:`entries`               roles, degrees and projects out of a section's lines
:mod:`resume`                the whole document into an :class:`~plan.ImportPlan`
:mod:`notes`                 a plain-text skills file (the v1 notes)
:mod:`dedupe`                recognising the same work in a second resume
:mod:`plan`                  what an import proposes — no database, nothing approved
:mod:`apply`                 the one module that writes, and writes only drafts
===========================  ==========================================================

Two properties hold across all of it. **Nothing an import produces is approved** —
:func:`aria_core.graph.facts.create_draft_fact` cannot be asked to. And **nothing the
importer cannot read is filled in**: an unreadable date, an unknown heading or a
missing employer becomes a question on the plan, never a plausible value.

Import from the submodule, never from this package: this file re-exports nothing, so that
importing a pure part of the importer (:mod:`classify`, :mod:`assemble`) does not load
:mod:`apply` and with it the database layer. The Resume Engine reads its own output back
through Stage 0 and must stay free of database code (DECISIONS 2026-10-16).
"""
