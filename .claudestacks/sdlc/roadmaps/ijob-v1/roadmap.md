---
status: approved
created: 2026-10-30
---

# Roadmap: ijob v1

## Vision

ijob is a terminal app (CLI + TUI) that helps software engineers find openings that fit them. It
collects postings from about 200 sources (company career pages behind hiring-system APIs, niche and
regional job boards, and URLs the user adds), filters them by role, posting date, company, salary,
country and free-text custom instructions, and ranks them against the user's PDF CV with
explanations. Every LLM step is a typed DSPy module behind its own model provider (Claude Agent SDK
or OpenRouter), measured with DeepEval, traced with OpenTelemetry into a local Langfuse and improved
with GEPA from the user's own feedback. Agents handle the open-ended work: finding a company's careers source, exploring custom
instructions, coaching the CV. ijob is also a learning-by-doing project for agentic systems and a
portfolio piece for AI engineering, so its internals stay explicit, tested and documented, not
hidden behind frameworks.

## Non-goals

- Roles outside software engineering (design, sales, ops…).
- LinkedIn, Indeed, Glassdoor and ZipRecruiter, and any source whose terms or robots.txt forbid
  automated access. JobStreet is a deep-link only.
- Bypassing anti-bot protection (Cloudflare challenges, CAPTCHAs), logged-in scraping, or using the
  user's accounts on job sites.
- Applying to jobs automatically or contacting recruiters on the user's behalf.
- A hosted or multi-user web service; ijob runs locally for one user.
- Building a claude.ai login flow. The `claude-sdk` provider uses the Claude Code login already on
  the machine; OpenRouter uses `OPENROUTER_API_KEY`.
- Editing the CV PDF in place. Revised CVs are exported as text/Markdown.
- OCR for scanned PDFs.
- LangGraph or other orchestration frameworks in v1.
