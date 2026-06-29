# 2026-06-29 - Local Git Push Policy

## Properties
- type: decision
- status: decided
- owner: User & Athena

## Decision
All git commits made by Athena must remain local only. Auto-pushing to GitHub is strictly forbidden.

## Why
To grant the user complete oversight and control over remote repository syncs and prevent unvetted automated pushes to GitHub.

## Alternatives Considered
- Auto-pushing on every feature completion (Rejected: Explicitly forbidden by user directive).

## Revisit Trigger
Revisit only if user explicitly revokes the directive in chat.

## AI Recall
When committing code changes, execute local commits only (`git commit`). Never execute `git push`.
