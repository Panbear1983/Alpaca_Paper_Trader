# Alpaca_Paper_Trader - agent rules

**Memory wing: `alpaca_paper_trader`.** All agents (Claude Code, Hermes, Codex, Antigravity) share ONE long-term
memory, the mempalace, reached through the `mempalace_*` tools. Full rules and wing table:
`~/.mempalace/MEMORY_PROTOCOL.md`.

Alpaca paper-trading TUI (public repo). The Hermes profile wanna_buffet trades here.

## Every task in this repo
1. Start: `mempalace_search(<what you are about to do>, wing="alpaca_paper_trader")` before planning.
2. Finish: one `mempalace_add_drawer(wing="alpaca_paper_trader", room=<decisions|backend|bugs|reference>)`:
   what was done, what was verified, what is still open, Peter's words verbatim where they matter.
   Dated facts (bug fixed, decision made, thing deployed) also go to `mempalace_kg_add`.
3. Never file secrets, tokens or financial figures.
4. The Hermes kanban board is live work state, not memory; do not copy cards into the palace.

## Standing rules for this repo
- This repo is PUBLIC on GitHub. Never commit .env, keys or account IDs; a live key was once found in an untracked file here.
- Alpaca credentials live in .env - never ask Peter to re-provide them.
