"""Blog Skills Agent — agent-kit port of google/adk-samples/agent-skills-tutorial.

Demonstrates 4 ways to package skills behind a single SkillCatalogToolset:
  1. Inline skill (Python literal)        — seo-checklist
  2. File-based skill (SKILL.md on disk)  — blog-writer
  3. External skill (same shape as 2)     — content-research-writer
  4. Meta skill (inline + embedded files) — skill-creator

Wire-up: combine all 4 Skill objects into an InMemorySkillRegistry and hand it
to Agent via `skills=`. The agent then exposes three auto-generated tools —
`list_skills` (L1 metadata), `load_skill` (L2 instructions),
`load_skill_resource` (L3 references) — exactly like ADK's SkillToolset.
"""

from __future__ import annotations

import os
from pathlib import Path

from agent_kit import (
    Agent,
    InMemorySkillRegistry,
    Skill,
    SkillFrontmatter,
)
from agent_kit.contrib.skills import FilesystemSkillRegistry

_HERE = Path(__file__).parent
_STORAGE_ROOT = _HERE / ".storage"


# ---------------------------------------------------------------------------
# Pattern 1 — Inline skill: written directly in Python.
# Best for: short, stable rules with no auxiliary files.
# ---------------------------------------------------------------------------
seo_skill = Skill(
    name="seo-checklist",
    frontmatter=SkillFrontmatter(
        name="seo-checklist",
        description=(
            "SEO optimization checklist for blog posts. Covers title tags,"
            " meta descriptions, heading structure, keyword placement,"
            " and readability best practices."
        ),
        version="1.0",
    ),
    body=(
        "# SEO Checklist\n\n"
        "When optimizing a blog post for SEO, check each item:\n\n"
        "1. **Title**: 50-60 chars, primary keyword near the start\n"
        "2. **Meta description**: 150-160 chars, includes a call-to-action\n"
        "3. **Headings**: H2/H3 hierarchy, keywords in 2-3 headings\n"
        "4. **First paragraph**: Primary keyword in first 100 words\n"
        "5. **Keyword density**: 1-2%, never forced or awkward\n"
        "6. **Paragraphs**: 2-3 sentences max, use bullet lists often\n"
        "7. **Links**: 2-3 internal + 3-5 external to authoritative sources\n"
        "8. **Images**: Alt text with keywords, compressed, descriptive names\n"
        "9. **URL slug**: Short, keyword-rich, hyphenated\n\n"
        "Review the content against each item and suggest specific improvements."
    ),
    files={},
    storage_root=_STORAGE_ROOT / "seo-checklist",
)


# ---------------------------------------------------------------------------
# Pattern 4 — Meta skill: inline definition with embedded reference files.
# Best for: self-extending agents that generate new capabilities on demand.
# `files` holds bytes the agent can fetch via `load_skill_resource(path=...)`.
# ---------------------------------------------------------------------------
_SKILL_SPEC = """\
# Agent Skills Specification

## SKILL.md Format
Every skill directory must contain a `SKILL.md` file.

### Frontmatter (YAML)
```yaml
---
name: my-skill-name          # kebab-case, max 64 chars
description: What this skill does.  # max 1024 chars
version: "1.0"               # semver-ish; optional, defaults to 0.0.0
---
```

### Body (Markdown)
The body contains the skill instructions — clear, step-by-step actions the
agent will follow when this skill is loaded.

### Directory Structure
```
my-skill-name/
  SKILL.md           # Required: metadata + instructions
  references/        # Optional: detailed reference docs (loaded on demand)
  assets/            # Optional: templates, data files
  scripts/           # Optional: executable scripts
```

### Key Rules
- Directory name MUST match the `name` field in frontmatter
- Name must be kebab-case: ^[a-z0-9]+(-[a-z0-9]+)*$
- Description is what the LLM uses to decide when to load the skill
- Keep instructions actionable — tell the agent WHAT to do
- Put detail in `references/<file>` and tell the agent to fetch them via
  `load_skill_resource`
"""

_EXAMPLE_SKILL = """\
# Example: Code Review Skill

```markdown
---
name: code-review
description: Reviews Python code for correctness, style, and performance.
  Checks for common bugs, PEP 8 compliance, and suggests optimizations.
version: "1.0"
---

# Code Review Instructions

When asked to review code:

## Step 1: Read the Guidelines
Use `load_skill_resource(name="code-review", path="references/review-checklist.md")`
to load the detailed checklist.

## Step 2: Analyze
Check the code against each item in the checklist.

## Step 3: Report
Provide findings organized by severity:
- **Critical**: Bugs, security issues
- **Warning**: Style violations, performance concerns
- **Info**: Suggestions for improvement
```
"""

skill_creator = Skill(
    name="skill-creator",
    frontmatter=SkillFrontmatter(
        name="skill-creator",
        description=(
            "Creates new agent-kit-compatible skill definitions from"
            " requirements. Generates complete SKILL.md files following the"
            " Agent Skills specification."
        ),
        version="1.0",
    ),
    body=(
        "# Skill Creator Instructions\n\n"
        "When asked to create a new skill, generate a complete SKILL.md file.\n\n"
        "1. Call `load_skill_resource(name=\"skill-creator\","
        " path=\"references/skill-spec.md\")` to read the format spec.\n"
        "2. Call `load_skill_resource(name=\"skill-creator\","
        " path=\"references/example-skill.md\")` to see a working example.\n\n"
        "Then follow these rules:\n"
        "1. Name must be kebab-case, max 64 characters\n"
        "2. Description must be under 1024 characters\n"
        "3. Instructions should be clear and step-by-step\n"
        "4. Put detailed domain knowledge in `references/*.md`, NOT inline\n"
        "5. Keep SKILL.md under 500 lines\n"
        "6. Output the complete file content the user can save directly\n"
    ),
    files={
        "references/skill-spec.md": _SKILL_SPEC.encode("utf-8"),
        "references/example-skill.md": _EXAMPLE_SKILL.encode("utf-8"),
    },
    storage_root=_STORAGE_ROOT / "skill-creator",
)


# ---------------------------------------------------------------------------
# Patterns 2 & 3 — File-based + "external": loaded from the skills/ directory.
# (In a real project, an "external" skill would be downloaded / git-cloned
#  into the same skills_root. The Python wiring is identical.)
# ---------------------------------------------------------------------------
def _load_filesystem_skills() -> list[Skill]:
    """Synchronously load every SKILL.md under app/skills/.

    FilesystemSkillRegistry.load() is async, but at startup we want concrete
    `Skill` objects to hand to InMemorySkillRegistry alongside the inline ones.
    """
    import asyncio

    fs_reg = FilesystemSkillRegistry(
        _HERE / "skills",
        storage_root=_STORAGE_ROOT,
    )

    async def _load_all() -> list[Skill]:
        names = [fm.name for fm in await fs_reg.list()]
        return [await fs_reg.load(n) for n in names]

    return asyncio.run(_load_all())


_FS_SKILLS = _load_filesystem_skills()


# ---------------------------------------------------------------------------
# Assemble: every Skill goes into one InMemorySkillRegistry.
# Agent(skills=registry) auto-wires SkillCatalogToolset with the three tools.
# ---------------------------------------------------------------------------
registry = InMemorySkillRegistry([seo_skill, skill_creator, *_FS_SKILLS])


INSTRUCTION = (
    "You are a blog-writing assistant with specialized skills.\n\n"
    "You have four skills available:\n"
    "- **seo-checklist**: SEO optimization rules (load for SEO review)\n"
    "- **blog-writer**: Writing structure and style guide (load for writing)\n"
    "- **content-research-writer**: Research methodology (load for research)\n"
    "- **skill-creator**: Generate new skill definitions (load to create skills)\n\n"
    "When the user asks you to write, research, or optimize a blog post:\n"
    "1. Load the relevant skill(s) to get detailed instructions\n"
    "2. Use `load_skill_resource` to access reference materials\n"
    "3. Follow the skill's step-by-step instructions\n"
    "4. Apply multiple skills together when appropriate\n\n"
    "When the user asks you to create a new skill:\n"
    "1. Load the skill-creator skill\n"
    "2. Read the specification and example references\n"
    "3. Generate a complete SKILL.md that follows the spec\n\n"
    "If the user asks for a skill you don't have, say so plainly — don't pretend.\n"
    "Always explain which skill you're using and why."
)


def build_agent(model: str | None = None) -> Agent:
    """Construct the root agent. `model` defaults to GOOGLE_MODEL env var
    or `gemini/gemini-2.5-flash` (LiteLLM provider-prefixed)."""
    return Agent(
        name="blog_skills_agent",
        model=model or os.environ.get("GOOGLE_MODEL", "gemini/gemini-2.5-flash"),
        instruction=INSTRUCTION,
        skills=registry,
        default_max_rounds=12,
    )


# Convenience top-level Agent for `python -m app.main` / interactive use.
root_agent = build_agent()
