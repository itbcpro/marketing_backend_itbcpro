# ADR 0002 — The application is English only

Status: accepted (2026-09-29)

## Context
An earlier draft of the plan added a Persian locale with RTL support. The product owner clarified that the whole application is English.

## Decision
- UI, API messages, documentation, comments and code are English only. No Arabic-script text may exist anywhere in the repository; `tools/check_language.py` enforces this in pre-commit and CI.
- Text is still kept out of code through catalogs: Django gettext (`locale/en`) and `next-intl` (`apps/web/messages/en.json`). This gives one place to edit copy and keeps the option to add a language later, but no other locale ships.
- Content the platform generates for a market (ads, posts, articles) may be in any language when a brief asks for it; that is output, not application UI.

## Consequences
No RTL layout work, no locale switcher, no second font family. Routing uses a single locale without a URL prefix.
