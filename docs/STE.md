# ASD-STE100: the apex-router default format

apex-router uses ASD-STE100 Simplified Technical English (STE) as the default format for text that agents read and write.
STE makes text short. It makes instructions clear for people and for models.
A sub-agent reads only its brief, so a clear brief prevents a wrong result.

## Where STE applies

| Surface | How | Off |
|---|---|---|
| pi, every session | `integrations/pi/apex-ste.ts` adds the rules to the system prompt | `APEX_STE=0` |
| pi sub-agents (`agent_spawn`) | The child system prompt gets the rules. The tool asks for a brief in STE form. | `APEX_STE=0` |
| Claude Code | `apex-router ste install-claude` adds the rules to `~/.claude/CLAUDE.md` between markers | `install.sh --no-ste`, or delete the block |
| Claude Code skill | `plugin/skills/ste/SKILL.md` (briefs for Agent and Workflow) | — |
| New docs in this repo | Write them in STE. Run `apex-router ste check` before the commit. | — |

The rules have one source: `src/apex_router/data/ste_rules.json`.
The Python side (`apex_router/ste.py`) and the pi side (`integrations/pi/lib/ste.ts`) read the same file.
A test (`tests/test_ste.py`) makes sure that both sides give the same text.

## Commands

```
apex-router ste rules                 # the block that agents get
apex-router ste brief                 # the sub-agent brief template
apex-router ste check FILE|- [--kind procedure|description] [--json]
apex-router ste install-claude [--path P]
```

`check` exits with 1 when it finds a problem. It reports four rules:

| Rule | Finding |
|---|---|
| length | A procedure sentence has more than 20 words, or a description sentence has more than 25 words. |
| word | A word from the replacement list, for example `utilize` (use `use`). |
| ing | A word that ends in -ing and is not in the permitted list. |
| passive | A form of "be" followed by a past participle. |

The check skips code blocks, tables, headings and inline code.

## Limits

- The check covers only the mechanical rules.
- It does not include the STE dictionary. ASD licenses the dictionary separately.
- A human review is necessary for full STE conformance.
- STE has no successor standard. apex-router follows the current issue of ASD-STE100.
- apex-router does not convert old docs at once. Convert a doc when you change it.
