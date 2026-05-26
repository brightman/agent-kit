# agent-skills-tutorial (agent-kit port)

A port of [google/adk-samples · agent-skills-tutorial](https://github.com/google/adk-samples/tree/main/python/agents/agent-skills-tutorial)
built with **agent-kit** instead of ADK.

It demonstrates the same four ways of packaging skills behind a single catalog
toolset — but in roughly half the code, because agent-kit exposes the catalog
as a first-class `Agent(skills=…)` argument.

## What you'll see

| Pattern | Skill | Implementation |
|---|---|---|
| 1. Inline | `seo-checklist` | `Skill(...)` constructed in Python (no files) |
| 2. File-based | `blog-writer` | `app/skills/blog-writer/SKILL.md` + `references/` |
| 3. External | `content-research-writer` | Same shape as 2 — in practice you'd `git clone` it in |
| 4. Meta | `skill-creator` | Inline `Skill(...)` with embedded reference files |

Behind the scenes, agent-kit auto-generates three tools (the same set ADK ships):

- `list_skills()` — L1 metadata, ~200 tokens, always in context
- `load_skill(name)` — L2 full SKILL.md instructions, loaded on demand
- `load_skill_resource(name, path)` — L3 reference files

This is **progressive disclosure**: the model sees ~200 tokens up front and
pulls in the rest only when it decides it needs them.

## Quick start

```bash
# From the repo root — install agent-kit with the LiteLLM extra
pip install -e .[litellm]

# Configure your API key (LiteLLM picks it up from env)
cd samples/agent-skills-tutorial
cp .env.example .env
# edit .env, set GOOGLE_API_KEY=...

# One-shot
PYTHONPATH=../.. python -m app.main \
  "I have a blog post titled 'Getting Started with Kubernetes'. Review it for SEO."

# Interactive REPL (multi-turn; prior_messages auto-stitched)
PYTHONPATH=../.. python -m app.main
```

> The `PYTHONPATH=../..` line is only needed while agent-kit isn't installed
> from PyPI — once it is, `pip install agent-kit[litellm]` makes the path
> unnecessary.

## Try the same five demo prompts as the ADK sample

Inside the REPL, type the number or paste the prompt:

| # | Prompt | What it demonstrates |
|---|---|---|
| 1 | `I have a blog post titled 'Getting Started with Kubernetes'. Can you review it for SEO?` | Inline skill (`seo-checklist`) loaded on demand |
| 2 | `Help me write a short introduction for a blog about Python async programming. Make it SEO-friendly.` | Multi-skill: `blog-writer` + `seo-checklist` |
| 3 | `Can you use your video-editing skill to create a thumbnail?` | Agent declines gracefully — no such skill |
| 4 | `OK, then use your content research skill to help me research async Python` | External skill with resource loading |
| 5 | `I need a new skill for reviewing Python code for security vulnerabilities. Can you create a SKILL.md?` | Meta skill — `skill-creator` generates a new SKILL.md |

After each turn, the CLI prints a one-line trace:

```
[trace] tool calls: list_skills, load_skill, load_skill_resource
[trace] rounds: 4, cancelled: False, error: False
```

…which is the bit you can't see in ADK's web UI without enabling tracing.

## Project layout

```
samples/agent-skills-tutorial/
├── README.md                ← this file
├── pyproject.toml
├── .env.example
└── app/
    ├── __init__.py
    ├── agent.py             ← all 4 skill patterns + Agent construction
    ├── main.py              ← CLI runner (one-shot + REPL)
    ├── test_agent.py        ← offline smoke test (no API key needed)
    └── skills/
        ├── blog-writer/
        │   ├── SKILL.md
        │   └── references/style-guide.md
        └── content-research-writer/
            ├── SKILL.md
            └── references/seo-guidelines.md
```

## How it maps to the ADK sample

| ADK construct | agent-kit equivalent |
|---|---|
| `google.adk.skills.models.Skill` | `agent_kit.Skill` |
| `google.adk.skills.models.Frontmatter` | `agent_kit.SkillFrontmatter` |
| `load_skill_from_dir(path)` | `FilesystemSkillRegistry(path).load(name)` (or just `Agent(skills=path)`) |
| `SkillToolset(skills=[...])` | `Agent(skills=registry)` — toolset is auto-wired |
| `Agent(model="gemini-2.5-flash", instruction=..., tools=[skill_toolset])` | `Agent(name=..., model="gemini/gemini-2.5-flash", instruction=..., skills=registry)` |
| `adk web` | `python -m app.main` (CLI + REPL) |

## Verifying without an API key

```bash
cd samples/agent-skills-tutorial
PYTHONPATH=../.. python -m pytest app/test_agent.py -v
```

The smoke test uses agent-kit's built-in `ScriptedProvider` shape to confirm:
- All 4 skills are registered
- The catalog toolset auto-advertises `list_skills` / `load_skill` / `load_skill_resource`
- `load_skill_resource` returns the right inline bytes for `skill-creator`'s
  embedded `references/skill-spec.md`
