# Local models and datasets for the weekend QLoRA run (2026-10)

## 0. Scope and labels

- Goal: find smaller, faster local models and QLoRA datasets for our measured local workload.
- Machine: Apple Silicon Mac, 48 GB unified memory. Serving: ollama and MLX.
- Label MEASURED: number comes from our data. The command is next to it.
- Label CLAIMED: number comes from a Hugging Face model card or API. The URL is next to it.
- Label ASSUMED: number is an input to an estimate. It has no source. Step 5 of the plan replaces it with a MEASURED value.
- No weights or datasets were downloaded. No local data was sent to any remote service.
- Hugging Face data came from `https://huggingface.co/api/models`, `/api/datasets` and the card `README.md` files. Fetched in 2026-10.
- Raw telemetry rows, prompts, session ids and local paths are not in this report.

## 1. Our workload

### 1.1 Commands used

| Id | Command |
|----|---------|
| C1 | `apex-router route-advise --json` |
| C2 | `apex-router pressure --json` |
| C3 | `apex-router labels report` |
| C4 | Count and percentile script over the offload telemetry log. It prints counts and percentiles only. Same fields as `src/apex_router/ornith/offload_telemetry.py`. |
| C5 | Same kind of script over the codeqa impact log and the codeqa validate log. These are the logs that `src/apex_router/ornith/offload_report.py` reads. |

### 1.2 Jobs, latency, tokens per lane

| Lane | Jobs | Latency | Prompt tokens | Output tokens | Pass rate | Source |
|------|------|---------|---------------|---------------|-----------|--------|
| codeqa `ask` | 539 questions | MEASURED p50 87 s, p90 162 s, sum 12.7 h (431 calls with a latency under 600 s) | MEASURED p50 9,119, p90 10,145 (n=539) | not logged | grounded citations 1,962 of 2,128 = 92.2 %. Hallucinated 166 = 7.8 %. Stale 0. | C5 |
| codeqa `validate` (claim check) | 386 runs, 5,241 claims checked. 4,374 local, 867 to frontier. | not logged | not logged | not logged | 45 claims struck = 0.9 % | C5 |
| review pre-filter (qwen3.8-27b) | 170 | MEASURED p90 43 s. p50 is 0 s, so many calls return at once. Sum 1.07 h. | MEASURED p50 1,479 (only 33 calls logged tokens) | MEASURED p50 402 (n=33) | 0 gated. All 170 escalated to frontier by design. | C4 |
| review pre-filter (ornith-35b) | 10 | MEASURED p50 4.1 s, p90 30 s | MEASURED p50 77 (n=7) | MEASURED p50 160 (n=7) | 0 gated | C4 |
| label judge | 544 votes (66.5 % coverage of 818 tasks) | not logged | not logged | not logged | accuracy on gold: no gold yet in the current store. Earlier bench: 0.98 on gold (n 56) for `ornith:35b`, see `docs/research/2026-10-08-label-judge-bench.md`. | C3 |
| codegen | 1 | MEASURED 1.3 s | MEASURED 52 | MEASURED 32 | 1 of 1 gated passed | C4 |
| adhoc | 11 | MEASURED p50 0.7 s (ornith), 12.6 s (qwen) | MEASURED p50 38 / 32 | MEASURED p50 4 / 11 | 0 gated | C4 |
| extract / summarize | 2 routed tasks | not logged | not logged | not logged | no signal (n=2 < 30) | C1 |
| embeddings (nomic-embed-text) | not in the offload log | not logged | not logged | n/a | n/a | not measured |

Facts from the task brief (not re-measured here, taken from the owner): thinking-ON codegen hangs, thinking-OFF codegen passed 4/4, review precision was 1/5 with good recall.

### 1.3 Other measured context

| Item | Value | Source |
|------|-------|--------|
| Proxy requests in the telemetry log | 43,262 rows | MEASURED `wc -l` on the telemetry log |
| Local requests in the pressure window | 0 of 149 | MEASURED C2 |
| Pressure level | GREEN | MEASURED C2 |
| Cheap-first escalation rate, review | 15.4 % (n=254) | MEASURED C1 |
| Cheap-first escalation rate, debug | 14.5 % (n=406) | MEASURED C1 |
| Labeled tasks | 16 of 818 (2 %). Gold 0 of target 100. | MEASURED C3 |

Limits of this data:
- The offload log is small (192 rows). Codegen has 1 row. Do not treat the codegen rate as proof.
- Review has no gate, so there is no measured precision in the logs. The 1/5 figure is from the brief.
- Token counts are missing for most review calls.

## 2. Top 3 lanes

| Rank | Lane | Why |
|------|------|-----|
| 1 | codeqa (`ask` + `validate`) | Most volume (5,780 jobs) and most latency cost (12.7 h of `ask` time, p50 87 s). Prompt is about 9 k tokens. |
| 2 | review pre-filter | Second latency cost (1.07 h, p90 43 s). Precision is poor (1/5, from the brief). The user waits for it. |
| 3 | label judge | 544 votes. Largest single voter in `labels report`. A faster judge raises label coverage. |

Codegen has the lowest volume in our data, but it is the lane the brief names as test-gated. The plan keeps one codegen gate for safety.

## 3. Candidate models

Rules: 0.5B-14B parameters, or MoE with 3B or fewer active parameters. MLX 4-bit in mlx-community or a GGUF build preferred. Non-commercial licenses rejected.

Columns "Params", "License", "Context", "Downloads" and "Updated" are CLAIMED from the Hugging Face API at `https://huggingface.co/api/models/<id>` and the `config.json` of each model. "MLX" is CLAIMED from `https://huggingface.co/api/models?author=mlx-community&search=<name>`.

### 3.1 Candidate table

| Id | Params | License | MLX 4-bit / GGUF | Context | Downloads | Updated | Published benchmark (CLAIMED, model card) |
|----|--------|---------|------------------|---------|-----------|---------|-------------------------------------------|
| Qwen/Qwen3-4B-Instruct-2507 | 4.02 B | apache-2.0 | `mlx-community/Qwen3-4B-Instruct-2507-4bit` (45,666 dl) | 262,144 | 2,979,778 | 2025-09-17 | LiveCodeBench v6 35.1, IFEval 83.4, BFCL-v3 61.9 (the card's right-hand column) |
| Qwen/Qwen2.5-Coder-7B-Instruct | 7.62 B | apache-2.0 | `mlx-community/Qwen2.5-Coder-7B-Instruct-4bit` (256,097 dl) | 32,768 | 1,536,463 | 2025-01-12 | Card has no table in the text we parsed. Not recorded. |
| Qwen/Qwen3.5-4B | 4.66 B | apache-2.0 | `mlx-community/Qwen3.5-4B-MLX-4bit` (39,761 dl) | 262,144 | 7,962,138 | 2026-03-02 | Card lists IFEval, LiveCodeBench, BFCL-V4 in an HTML table. Values not parsed. Read the card before use. |
| Qwen/Qwen3.5-9B | 9.65 B | apache-2.0 | `mlx-community/Qwen3.5-9B-MLX-4bit` (31,846 dl) | 262,144 | 8,262,238 | 2026-03-02 | Same as Qwen3.5-4B. Values not parsed. |
| Qwen/Qwen3-8B | 8.19 B | apache-2.0 | `mlx-community/Qwen3-8B-4bit` (104,678 dl) | 40,960 | 9,336,872 | 2025-07-26 | None in the text we parsed. Hybrid thinking model. |
| microsoft/Phi-4-mini-instruct | 3.84 B | mit | `mlx-community/Phi-4-mini-instruct-4bit` (2,614 dl); GGUF from unsloth, bartowski | 131,072 | 370,990 | 2025-12-10 | Card lists HumanEval, MBPP, IFEval. Values not parsed. |
| HuggingFaceTB/SmolLM3-3B | 3.08 B | apache-2.0 | GGUF from ggml-org, unsloth, bartowski. No MLX count checked. | not read | 754,302 | 2025-09-10 | Not recorded. |
| Qwen/Qwen3-30B-A3B-Instruct-2507 (MoE) | 30.53 B total, 8 experts per token (about 3.3 B active, CLAIMED by Qwen; slightly over the 3 B rule) | apache-2.0 | `mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit` (12,861 dl) | 262,144 | 1,066,035 | 2025-09-17 | LiveCodeBench v6 35.8 to 45.2 depending on column; IFEval 82.3 to 84.7 (the card compares several models. Check the column for this model.) |
| Qwen/Qwen3-Coder-30B-A3B-Instruct (MoE) | 30.53 B total, same active size | apache-2.0 | `mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit` (20,516 dl) | 262,144 | 468,166 | 2025-12-03 | Not recorded. |
| ibm-granite/granite-4.0-h-tiny (MoE) | 6.94 B total, 6 experts per token | apache-2.0 | `mlx-community/granite-4.0-h-tiny-4bit` (1,343 dl) | 131,072 | 42,925 | 2025-11-03 | Card lists IFEval. Value not parsed. |

Source URLs: `https://huggingface.co/<id>` for every row. Example: `https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507`.

### 3.2 Rejected or flagged

| Model | Decision | Reason |
|-------|----------|--------|
| google/gemma-3-4b-it | Excluded | License tag is `gemma` (custom terms with use rules). We prefer apache-2.0 or MIT. |
| rmems/code-review-preference-pairs (dataset) | Rejected | Card says `project_training_policy: blocked` and "research only". See section 4. |
| Non-commercial licenses | None found | No candidate in section 3.1 has a non-commercial license. |

### 3.3 Up to 4 candidates per top lane

| Lane | 1 | 2 | 3 | 4 |
|------|---|---|---|---|
| codeqa (9 k prompt, cited answers) | Qwen3-4B-Instruct-2507 | Qwen3.5-4B | Qwen3-30B-A3B-Instruct-2507 | Phi-4-mini-instruct |
| review pre-filter | Qwen3-4B-Instruct-2507 | Qwen2.5-Coder-7B-Instruct | Qwen3-Coder-30B-A3B-Instruct | Qwen3.5-9B |
| label judge | Qwen3-4B-Instruct-2507 | Qwen3.5-4B | Qwen3-8B (thinking OFF) | Phi-4-mini-instruct |
| codegen (kept as a safety lane) | Qwen2.5-Coder-7B-Instruct | Qwen3-4B-Instruct-2507 | Qwen3-Coder-30B-A3B-Instruct | Qwen3-8B (thinking OFF) |

Notes:
- `Qwen3-4B-Instruct-2507` has no thinking mode. This avoids the thinking-ON hang that we measured on codegen.
- `Qwen2.5-Coder-7B-Instruct` has a 32,768 token context. This covers the measured codeqa prompt (p90 10,145 tokens).
- The two 30B MoE models have about 3.3 B active parameters. They need about 17 GB at 4-bit (ASSUMED: 30.5 B × 0.5 byte). They are not smaller than `ornith-35b`. Use them only as a quality reference.
- A published benchmark does not predict our lanes. Only the local gates in section 6 decide.

## 4. Datasets for the weekend QLoRA

Columns "Size", "License" and "Updated" are CLAIMED from `https://huggingface.co/api/datasets/<id>` and `https://datasets-server.huggingface.co/size?dataset=<id>`. Format is CLAIMED from the card where the card states it. Otherwise it says "instruction/response, not checked".

### 4.1 Training sets (8)

| Id | Size (rows) | License | Format | Lane |
|----|-------------|---------|--------|------|
| ronantakizawa/github-codereview | 355,807 (card: 167 K positive triplets, 51 K negative "No issues found.") | `other` in the tag. Card says all source repos use permissive licenses. Read the card terms before use. | code chunk, reviewer comment, revised code | review (the negatives train "do not comment", which targets our 1/5 precision) |
| bigcode/self-oss-instruct-sc2-exec-filter-50k | 50,661 | odc-by | instruction/response, execution-filtered with tests (per dataset name), not checked | codegen |
| nvidia/OpenCodeInstruct | 1,400,000 | cc-by-4.0 | instruction/response with unit-test data (not checked) | codegen |
| ise-uiuc/Magicoder-OSS-Instruct-75K | 75,197 | mit | instruction/response, not checked | codegen. Outputs may come from a hosted model. Check its terms. |
| OpenCoder-LLM/opc-sft-stage2 | 436,347 | mit | instruction/response, not checked | codegen, general code |
| m-a-p/CodeFeedback-Filtered-Instruction | 156,526 | apache-2.0 | instruction/response, not checked | codegen, code Q&A (codeqa style answers) |
| Salesforce/xlam-function-calling-60k | gated (auto), size not read | cc-by-4.0 | JSON function-call rows, not checked | extract (strict JSON output) |
| Team-ACE/ToolACE | 11,300 | apache-2.0 | tool-call conversations, not checked | extract (strict JSON output) |

### 4.2 Eval-only or rejected

| Id | Size | License | Use |
|----|------|---------|-----|
| code-review-bench/code-review-bench | 1,271 rows (136 expert golden issues over 50 PRs, offline split) | cc-by-4.0 | Review eval. Do not train on it. |
| evalplus/mbppplus | 378 | apache-2.0 | Codegen eval with extra tests. Do not train on it. |
| NousResearch/json-mode-eval | 100 | not stated in the tags | Extract eval. Do not train on it. |
| rmems/code-review-preference-pairs | not training-ready | apache-2.0 tag, but card says training blocked | REJECTED. The card forbids use for model weight updates. |

### 4.3 Gaps

| Lane | Gap |
|------|-----|
| label judge | No public dataset fits our label schema. We have 100 model-made gold labels. This is too few to train. Keep `ornith:35b` as judge unless a zero-shot bench passes. |
| codeqa | No public dataset for cited code Q&A over our repos. Test zero-shot only. Do not train. |
| embeddings | Out of scope. No change to nomic-embed-text. |

## 5. QLoRA hours on 48 GB

### 5.1 Formula

```
tokens_total = N_examples x avg_tokens_per_example x epochs
hours        = tokens_total / tokens_per_second / 3600
```

Inputs are ASSUMED until the calibration run (plan step 5) gives a MEASURED tokens_per_second.
`src/apex_router/ornith/training/train.sh` sets LoRA rank 16, 300 iterations, learning rate 1e-5 as defaults. We override iterations from the formula.

Memory (ASSUMED): 4-bit weights = params x 0.5 byte. Qwen3-4B: about 2 GB. Qwen2.5-Coder-7B: about 3.8 GB. Add LoRA states and activations with gradient checkpointing and max sequence 2,048. Both fit in 48 GB. Unload the local serving models during training to avoid contention.

### 5.2 Pair A: Qwen3-4B-Instruct-2507 + review/extract/codegen mix

| Input | Value | Label |
|-------|-------|-------|
| N_examples | 18,000 | plan choice |
| avg_tokens_per_example | 900 | ASSUMED |
| epochs | 1 | plan choice |
| tokens_per_second | 700 | ASSUMED |
| tokens_total | 18,000 x 900 x 1 = 16.2 M | computed |
| hours (1 epoch) | 16.2 M / 700 / 3600 = 6.4 h | computed |
| hours (2 epochs) | 12.9 h | computed |

### 5.3 Pair B: Qwen2.5-Coder-7B-Instruct + codegen mix

| Input | Value | Label |
|-------|-------|-------|
| N_examples | 12,000 | plan choice |
| avg_tokens_per_example | 700 | ASSUMED |
| epochs | 1 | plan choice |
| tokens_per_second | 350 | ASSUMED (about half of the 4B rate, from the parameter ratio 7.62 / 4.02 = 1.9) |
| tokens_total | 12,000 x 700 x 1 = 8.4 M | computed |
| hours (1 epoch) | 8.4 M / 350 / 3600 = 6.7 h | computed |
| hours (2 epochs) | 13.3 h | computed |

Sensitivity: if tokens_per_second is 2x lower than assumed, the hours double. Pair A 2 epochs = 25.8 h. Pair B 2 epochs = 26.7 h. Both still fit in the 60 h budget, but not together with all evals.

## 6. Weekend plan (60 h budget)

### 6.1 Choice

| Item | Choice |
|------|--------|
| Primary model | Qwen3-4B-Instruct-2507 (MLX 4-bit). Fast, no thinking mode, long context. |
| Second model | Qwen2.5-Coder-7B-Instruct (MLX 4-bit). Codegen and review reference. |
| Zero-shot only (no training) | Qwen3.5-4B and Phi-4-mini-instruct, as challengers on codeqa and judge. |
| Dataset mix for Pair A (18,000) | 7,000 github-codereview (keep at least 35 % negatives, above the 23 % in the source) + 4,000 xlam-60k and ToolACE (strict JSON) + 5,000 self-oss-instruct-sc2-exec-filter + 2,000 CodeFeedback-Filtered (answer style) |
| Dataset mix for Pair B (12,000) | 8,000 self-oss-instruct-sc2-exec-filter + 2,000 OpenCodeInstruct + 2,000 opc-sft-stage2 |
| Replay | Add 10-20 % general replay data. `src/apex_router/ornith/training/prepare_data.py` needs it. Expand `probe_general.jsonl` to about 200 frozen prompts. |
| Eval-only data | code-review-bench (offline), mbppplus, json-mode-eval. Remove them from the train mix. |

Reason for the mix: our review gap is precision, not recall. Negative examples teach the model to stay silent on clean code. Our codegen gate needs tests, so only execution-filtered data enters Pair B.

### 6.2 Steps

1. Run a dry run: `src/apex_router/ornith/training/train.sh --dry-run` with `ORNITH_TRAIN_BASE` set to the MLX base. Make sure the data and `attestation.json` are present.
2. Download the models and datasets (not done in this research task). Build the mix with `prepare_data.py`. Run the contamination scan.
3. Bench zero-shot first (no training). Run the lane gates in the table below for the 4 candidate models. Record latency and quality.
4. Stop here for any lane where the zero-shot model already meets the gate. Do not train for it.
5. Calibrate: run 50 iterations of `mlx_lm.lora` on each pair. Record tokens_per_second. Recompute the hours with the formula in 5.1.
6. Train Pair A (about 6-13 h), then Pair B (about 7-13 h). Run them one after the other, not at the same time.
7. Fuse each adapter. Serve through ollama or MLX. Run the gates in the table below.
8. Keep the incumbent for any lane where the new model fails a gate.

### 6.3 Time budget

| Block | Hours |
|-------|-------|
| Download, build mix, contamination scan | 4 |
| Zero-shot bench of 4 models | 6 |
| Calibration | 1 |
| Pair A training (2 epochs, upper estimate) | 13 |
| Pair B training (2 epochs, upper estimate) | 13 |
| Fuse, convert, serve, final bench | 8 |
| Reserve (slower throughput, re-runs) | 15 |
| Total | 60 |

### 6.4 Local eval gates (existing benches)

| Lane | Gate | Pass rule |
|------|------|-----------|
| codegen | `apex-router skill-bench` on `benchmarks/codegen_probe.jsonl` (3 tasks) and `benchmarks/codegen_hard.jsonl` (8 tasks). Thinking OFF. | Pass all that the incumbent passes. No hang. Pass count is small (11), so also run `mbppplus` as an extra check. |
| label judge | `apex-router labels judge-bench` on the 100 gold labels | Accuracy on gold no lower than the incumbent `ornith:35b` (0.98, n 56 on the earlier bench). Wide interval: n is small. |
| codeqa | codeqa grounding rate from the codeqa impact log, on a fixed set of questions | Grounded at least 92.2 % (MEASURED baseline). Hallucinated no higher than 7.8 %. |
| review | No existing precision bench in the repo. Use `code-review-bench` offline split (136 golden issues) as an external check. Also replay the labeled review tasks. | Precision above the 1/5 baseline with recall not lower. Add a repo bench for this before the weekend. |
| regression | `probe_general.jsonl` (expand to about 200 prompts) | No drop against the base model. |
| speed | p50 and p90 latency for the same prompts | At least 3x faster than the current review p90 of 43 s (target, not a measured result). |

### 6.5 Success rule

- A model replaces the incumbent in a lane only when it passes that lane's gate and is faster.
- If no model passes, keep the incumbent. Write the result in a new file under `docs/research/`.

## 7. Open problems

| Id | Problem |
|----|---------|
| 1 | The offload log is small (192 rows, 1 codegen row). The workload sample is thin. |
| 2 | Judge latency and token sizes are not logged. Codeqa output tokens are not logged. |
| 3 | No review precision bench exists in the repo. |
| 4 | Training tokens_per_second is ASSUMED. Step 5 of the plan fixes this. |
| 5 | Benchmark values for Qwen3.5, Phi-4-mini, Qwen2.5-Coder and Granite were not parsed. Read the cards. |
| 6 | Dataset formats were not checked, because no data was downloaded. |
| 7 | `github-codereview` has the license tag `other`. Check the terms before training. |
| 8 | Gold labels are model-made (Opus). Judge accuracy is agreement with Opus, not with the owner. |
