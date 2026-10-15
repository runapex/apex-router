# TEST PROCEDURE — PI SUB-AGENTS AND HANG GUARD

**Language:** ASD-STE100 Simplified Technical English (writing rules of the current issue). The text was not
examined with the licensed STE dictionary or an STE checker. Examine it with one before external release.

**Related document:** `DESIGN-pi-subagents.md`.

**Technical names and technical verbs used in this document** (STE rules 1.5 and 1.12): pi, bash, Codex,
JSON, event, session ID, process, process group, grandchild process, SIGTERM, SIGKILL, timeout, terminal,
extension, tool call, "run" (a computer process), "kill" (stop a computer process with a signal).

---

## TASK 1 — P0 PLATFORM CHECKS (DONE 2026-10-14)

### 1. Reason for the job

Use this procedure to find if pi can operate sub-agents safely.
Do this procedure before you start work on P1 and P2.

### 2. Job set-up information

A. Equipment

| Item | Value |
|---|---|
| pi | version 0.85.1 |
| Python | python3 |
| Provider | foundry |
| Model | it-entra-claude-haiku-4-5 |
| Test script | `integrations/pi/p0/p0_checks.py` |
| Test extension | `integrations/pi/p0/p0-defer.ts` |

B. Results file: `~/.apex-router/pi-p0/results.jsonl`. The script adds one line for each check.

### 3. Procedure

CAUTION: DO NOT STOP THE SCRIPT DURING CHECK 4 OR CHECK 5. IF YOU STOP IT, A "sleep 240" PROCESS CAN
STAY IN MEMORY. FIND IT WITH "ps" AND KILL IT.

A. Prepare the test

1. Open a terminal.
2. Go to the directory `~/dev/apex-router`.
3. Make sure that the command `pi --version` shows 0.85.1.

B. Check 1 — deferred message

1. Type this command: `python3 integrations/pi/p0/p0_checks.py --only 1`
2. Make sure that the result line shows `"pass": true`.
3. Make sure that `assistant_texts` shows `WAITING`, then `GOT-PING`.
4. Make sure that `sendMessage_threw` shows `false`.

NOTE: `GOT-PING` shows that the agent started a new turn. The timer sent the message 3 seconds after the
tool call was complete.

C. Check 2 — liveness of the JSON stream

1. Type this command: `python3 integrations/pi/p0/p0_checks.py --only 2`
2. Make sure that the result line shows `"pass": true`.
3. Make sure that `ticking.max_gap_s` is less than 15.
4. Record the value of `silent.max_gap_s`.

NOTE: A command that writes no output gives no events. The gap is then equal to the duration of the command.

D. Check 3 — resume after SIGKILL

1. Type this command: `python3 integrations/pi/p0/p0_checks.py --only 3`
2. Make sure that the result line shows `"pass": true`.
3. Make sure that `killed_mid_tool` shows `true`.
4. Make sure that `resume_answer` shows `PELICAN-7`.

E. Check 4 — SIGTERM, then SIGKILL

1. Type this command: `python3 integrations/pi/p0/p0_checks.py --only 4`
2. Make sure that the result line shows `"pass": true`.
3. Make sure that `alive_after_group_kill` shows `false`.
4. Record the values of `same_group` and `pi_exited_on_sigterm`.

F. Check 5 — SIGKILL only

1. Type this command: `python3 integrations/pi/p0/p0_checks.py --only 5`
2. Make sure that the result line shows `"pass": true`.
3. Make sure that `alive_after_sigkill_only` shows `true`.

NOTE: In check 5, "pass" means that the risk is real. The grandchild process stays alive after SIGKILL.

### 4. Results (2026-10-14)

| Check | Requirement | Expected | Actual | Result |
|---|---|---|---|---|
| 1 | A deferred message starts a new agent turn | WAITING, then GOT-PING | WAITING, GOT-PING; 2 agent starts | PASS |
| 2 | The stream shows events during a long command | max gap < 15 s | 10.0 s (ticking); 60.0 s (silent) | PASS |
| 3 | A killed child keeps its context | PELICAN-7 | PELICAN-7 | PASS |
| 4 | SIGTERM stops the grandchild process | not alive | not alive; `same_group` false; pi stopped on SIGTERM | PASS |
| 5 | SIGKILL only leaves an orphan | alive | alive (orphan bash and sleep) | PASS (risk is real) |

### 5. Changes to the design from these results

1. The completion message from a timer operates correctly. The fallback is not necessary.
2. A command with no output gives no events. Set the idle limit longer than the bash timeout of the child.
3. The bash tool puts each command in a different process group. A kill of the pi process group does not
   stop these commands.
4. SIGKILL alone leaves orphan processes. Use this sequence to cancel a job:
   1. Record the process group of each descendant process.
   2. Send SIGTERM to the pi process.
   3. Wait 10 seconds.
   4. Send SIGKILL to each process group that you recorded.

---

## TASK 2 — P1 HANG GUARD ACCEPTANCE TEST (NOT DONE: THE HANG GUARD IS NOT BUILT)

### 1. Reason for the job

Use this procedure to make sure that the hang guard stops each type of bash hang.
Make sure that the hang guard does not block a command that operated correctly before.

### 2. Job set-up information

A. Equipment: the P1 build of `integrations/pi/apex-hangguard.ts`, the session logs in
`~/.pi/agent/sessions`, and the replay script (part of P1).

B. Test data: the 54 bash commands that stopped with "Command aborted" (36) or "Command timed out" (18)
in the session logs.

### 3. Procedure

A. Default timeout

1. Start pi with the hang guard extension.
2. Give a bash tool call with no timeout.
3. Make sure that the hang guard log shows the default timeout (600 seconds).
4. Give a bash tool call with a timeout of 30 seconds.
5. Make sure that the hang guard does not change this timeout.

B. Standard input

1. Give the command `codex exec "echo"` with no input redirection.
2. Make sure that the hang guard adds `< /dev/null` to the command.
3. Give the command `codex exec "echo" < /dev/null`.
4. Make sure that the hang guard does not change this command.

C. SSH

1. Give the command `ssh devtest22 true`.
2. Make sure that the hang guard adds `-o BatchMode=yes` and `-o ConnectTimeout=15`.
3. Make sure that a host that asks for a password gives an error in less than 20 seconds.

D. Sleep polls

1. Start one sub-agent job.
2. Give the command `sleep 300`.
3. Make sure that the hang guard blocks the command.
4. Make sure that the reason tells the agent to use `agent_wait`.
5. Stop all jobs.
6. Give the command `sleep 300` again.
7. Make sure that the hang guard does not block the command.

E. Replay of the history

1. Do the replay of the 54 test commands through the hang guard.
2. Make sure that each command has a timeout or a rewrite.
3. Do the replay of 500 commands that operated correctly. Select them at random from the same logs.
4. Make sure that the hang guard blocks none of these 500 commands.

### 4. Pass criteria

All steps in paragraph 3 are correct. The hang guard log shows each change to each command.

---

## TASK 3 — P2 SUB-AGENT ACCEPTANCE TEST (NOT DONE: THE TOOLS ARE NOT BUILT)

### 1. Reason for the job

Use this procedure to make sure that each sub-agent job stops in a known state.

### 2. Job set-up information

A. Equipment: the P2 build of `integrations/pi/apex-agents.ts`, and the fake child process
`tests/fixtures/fake-pi.mjs`. The fake child writes JSON events from a script. It does not use a model.

### 3. Procedure

A. Normal job

1. Start a job with `agent_spawn`. Use the fake child in the "normal" mode.
2. Make sure that `agent_spawn` gives a job ID immediately.
3. Make sure that the parent agent receives the completion message. Do not use `sleep`.
4. Make sure that `agent_result` gives the final text and the path of the event log.

B. Hung job

WARNING: DO NOT USE A REAL MODEL FOR THIS TEST. A REAL CHILD CAN CONTINUE TO USE TOKENS UNTIL THE
TIMEOUT.

1. Start a job with the fake child in the "silent hang" mode. Set `idle_s` to 20.
2. Make sure that the job state is `timed_out(idle)` in less than 25 seconds.
3. Make sure that no fake child process stays in memory.

C. Wall-clock limit

1. Start a job with the fake child in the "slow but alive" mode. Set `timeout_s` to 30.
2. Make sure that the job state is `timed_out(wall)` in less than 35 seconds.
3. Make sure that the result gives the command to resume the job.

D. Crash

1. Start a job with the fake child in the "crash" mode.
2. Make sure that the job state is `failed`.
3. Make sure that the result shows the error text of the child.

E. Cancel and orphans

1. Start a job with the fake child in the "grandchild" mode. In this mode, the fake child starts a
   grandchild process in a different process group.
2. Use `agent_cancel` on the job.
3. Make sure that the job state is `killed`.
4. Make sure that the grandchild process is not alive.
5. Do steps 1 to 4 again with the fake child in the "ignores SIGTERM" mode.

F. Limit and pressure

1. Set the limit to 4 jobs.
2. Start 6 jobs.
3. Make sure that 4 jobs are `running` and 2 jobs are `queued`.
4. Write `RED` into a test copy of `pressure.json`.
5. Start 1 more job.
6. Make sure that the job state is `queued(pressure)`.

G. Restart of the parent

1. Start a job in the "slow but alive" mode.
2. Stop the parent pi.
3. Start the parent pi again.
4. Make sure that `agent_status` shows the job as `running`.
5. Kill the child process.
6. Start the parent pi again.
7. Make sure that `agent_status` shows the job as `failed`.

### 4. Pass criteria

All steps in paragraph 3 are correct. Each job stops in one of these states: `done`, `failed`, `killed`,
`timed_out(idle)`, `timed_out(wall)`. No child process stays in memory after the test.
