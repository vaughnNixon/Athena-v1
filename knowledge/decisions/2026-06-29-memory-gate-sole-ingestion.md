# 2026-06-29 - Memory Gate Sole Ingestion Path

## Properties
- type: decision
- status: decided
- owner: User & Athena

## Decision
All cognitive memory ingestion must pass strictly through the Memory Gate and SQLite database (`athena_v1.db`). Markdown files (reports, decision logs, persona docs) serve as human audit reporting layers and static configurations, never as unindexed prompt memory.

## Why
To preserve SQLite + AAL as the single source of truth, prevent context window inflation, and eliminate memory duplication.

## Alternatives Considered
- Using raw Markdown files directly as conversational retrieval memory (Rejected: Wastes massive tokens and degrades LLM performance).

## Revisit Trigger
Revisit if architectural scale or memory engine backend undergoes major refactoring.

## AI Recall
When discussing memory architecture, assume Memory Gate is the sole ingestion path unless explicit structural changes are approved.
