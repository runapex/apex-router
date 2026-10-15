---
name: ste
description: Use when you write instructions, plans, reports, commit messages or a sub-agent brief (Agent, Workflow, pi agent_spawn). ASD-STE100 Simplified Technical English is the apex-router default format. Short sentences, one instruction in each step, active voice, one word for one meaning. Also use it to check or shorten a text.
---

# STE: the apex-router default format

Write text for agents in ASD-STE100 Simplified Technical English (STE).
STE makes text short and makes instructions clear.
A sub-agent reads only its brief. A clear brief prevents a wrong result.

## Rules

Run `apex-router ste rules` to show the full rules block.

1. Procedures: use the imperative. Write one instruction in each sentence. Use 20 words or fewer.
2. Descriptions: use 25 words or fewer in each sentence.
3. Use the active voice. Name who or what does the action.
4. Use one word for one meaning. Use `apex-router ste rules` for the word list.
5. Use "must" for requirements.
6. Put a WARNING or a CAUTION before the step it applies to.
7. Keep code, paths, commands and technical names unchanged.
8. Delete filler words and repetition.

## Sub-agent brief

Write every brief in this form. Run `apex-router ste brief` to show the template.

| Part | Content |
|---|---|
| TASK | One sentence. The result that the sub-agent must give. |
| CONTEXT | Only the facts that the sub-agent needs: paths, versions, data that you have. |
| STEPS | Numbered steps, if the order is important. |
| LIMITS | The files and actions that are not permitted. The time or cost limit. |
| RESULT | The format of the answer. For example: a table, a list with file:line, or GO/NO-GO. |

## Check a text

1. Run `apex-router ste check FILE` (or `- ` for stdin).
2. Fix each finding: length, word, ing or passive.
3. Run the check again. Exit code 0 means no findings.

The check covers only the mechanical rules.
It does not include the licensed STE dictionary.
A human review is necessary for the full standard.

## Scope

- Use STE for instructions, plans, reports, briefs and commit messages.
- Do not change quoted user text, code, logs or error messages.
- Answers to the user in chat also use STE, unless the user asks for a different style.
