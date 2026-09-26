# EMBER — Full System Architecture (v1)
### Embedded Modular Brain for Execution & Reasoning

*Companion to `ember_architecture_brief.md` (Stage 1 setup doc). That doc is your action checklist for getting a model running. This doc is the full reference architecture — every subsystem, how they connect, and why, grounded in the underlying mechanics rather than treated as black boxes.*

*Legend: **[DECIDED]** = locked in. **[PROPOSED]** = recommended, open to change. **[OPEN]** = genuinely undecided, flagged for later.*

---

## 0. The One-Paragraph Version

Ember is a loop, not a brain. A piece of orchestrator code you write takes user input, decides what context the LLM needs (system prompt + retrieved memories + tool descriptions + recent history), sends it to a local model for one pass of "predict the next tokens," inspects the output, executes any tool calls with real code, feeds results back if needed, returns a reply, writes anything worth keeping to a memory store, and optionally speaks it aloud. Every subsystem below is a piece of that loop.

```text
                              ┌─────────────┐
                              │    USER      │
                              └──────┬───────┘
                                     │
                          ┌──────────▼───────────┐
                          │   Voice / Text I/O     │  §4
                          └──────────┬───────────┘
                                     │
                        ┌────────────▼─────────────┐
                        │    EMBER ORCHESTRATOR      │  §2  ← your code, the "brain stem"
                        └──┬────────┬────────┬─────┬─┘
                           │        │        │     │
                ┌──────────▼──┐ ┌──▼─────┐ ┌▼────┐ ┌▼──────────┐
                │  LLM Engine  │ │ Memory │ │Tools│ │  Resource  │
                │   §1         │ │  §3    │ │ §5  │ │  Manager   │
                │              │ │        │ │     │ │   §6       │
                └──────────────┘ └────────┘ └─────┘ └─────┬──────┘
                                                            │
                                                  ┌─────────▼─────────┐
                                                  │  Tier 2: New Laptop │
                                                  │  (escalation only)  │
                                                  └─────────────────────┘
```

---

## 1. LLM Engine — the reasoning core

**What it is, mechanically:** a quantized weights file + an inference engine that runs attention/feedforward math over it, one token at a time, to turn "context in" into "text out." (See theory: tokens, quantization, attention.)

**[DECIDED — hardware constraint]** CPU-bound inference. i7-8550U, 16GB RAM, MX150 2GB VRAM. No meaningful full-GPU inference; partial layer offload only, treated as a bonus not a plan.

**[PROPOSED] Stack:**
- Engine: **Ollama** (wraps llama.cpp, OpenAI-compatible local API at `localhost:11434`)
- Model: **Qwen2.5-7B-Instruct** (Q4_K_M) as primary candidate — strong tool-calling support, which matters once §5 is active. **Phi-3.5-mini** as a faster fallback if 7B feels sluggish on this CPU.
- Quantization: **Q4_K_M** — the accuracy/size sweet spot given 16GB total RAM has to also hold OS + (eventually) memory system + voice models.

**[OPEN]** Whether Ember eventually runs *multiple* specialized models (e.g., a fast small model for simple queries, a larger one for hard reasoning, routed by the orchestrator) — a real pattern in more mature local-AI setups, but adds real complexity. Not needed at Stage 1; revisit once the single-model loop feels limiting, not before.

**Interface contract:** everything above this component talks to it through one adapter (`llm_client.py`) — a function like `generate(prompt, context) -> text`. This is what makes the model itself swappable without touching the orchestrator, memory, or tool code. Treat this boundary as sacred — don't let orchestrator logic leak into the LLM client, or vice versa.

---

## 2. Orchestrator — the actual "Ember"

**What it is:** plain code (Python, per your existing comfort) implementing the loop from §0. This is the one component that is genuinely *yours* — no pretrained model does this job.

**Concrete responsibilities, per turn:**
1. Receive input (text now, transcribed speech later — §4 normalizes both to text before this point)
2. Build the prompt: system prompt (always) + retrieved memories (§3, conditionally) + tool descriptions (§5, conditionally) + trimmed recent history (§7 context management)
3. Call the LLM engine (§1)
4. Parse output: plain reply, or a tool-call request?
5. If tool call: check permissions (§5), execute, feed result back into context, re-call the LLM (step 3 again)
6. Return final reply to I/O layer (§4)
7. Decide what's worth writing to memory (§3), store it
8. Log the interaction (useful for debugging *and* for the episodic memory type in §3)

**[OPEN]** Whether to hand-roll this loop (full control, more code to maintain — matches your "understand every piece" learning goal well) versus use an existing agent framework (LangChain, LlamaIndex's agent tooling, etc. — faster start, but you inherit someone else's abstractions and debugging becomes harder when something misbehaves).

**Recommendation for you specifically:** hand-roll it, at least through Stage 1–3. Given you just spent this much effort actually understanding the mechanics, a framework would hide exactly the parts you now understand — and your own doc's "maintainability" priority (§21.8 of the original brief) argues for code you fully own, at least until the hand-rolled version is genuinely limiting you.

---

## 3. Memory — RAG, not magic recall

**What it is, mechanically:** an embedding model converts text to vectors; a vector database stores them; retrieval finds semantically close matches to inject into context at generation time. The LLM itself is stateless — memory lives entirely outside it.

**Mapping your doc's memory types to real mechanisms:**

| Memory type (from original doc) | Mechanism | Notes |
|---|---|---|
| Working memory | Just the current context window | Not persisted — it's the "RAM," gone after the turn/session ends unless written elsewhere |
| Conversational memory | Recent turns kept in context, or summarized (§7) | Short-lived, mostly window-based |
| Long-term memory | Vector DB, embedded + retrieved via RAG | The real persistent layer |
| User preferences | Could be vector DB, but often better as a small structured store (plain key-value or SQLite table) — preferences are usually explicit facts, not fuzzy semantic matches | Don't force everything into embeddings; some things are just database rows |
| Project memory | Vector DB, likely namespaced/tagged by project | Retrieval scoped to "what project is this conversation about" |
| Episodic memory | Vector DB entries tagged with timestamps/event type | Retrieval can weight by recency, not just similarity |

**[PROPOSED] Stack:**
- Embedding model: a small local one (e.g., `nomic-embed-text` or similar, runs easily on CPU, milliseconds per call — this is cheap compared to the LLM)
- Vector store: something lightweight to start — **Chroma** (simple, embedded, no separate server process) or even a hand-rolled SQLite table with a vector similarity extension, given you're prioritizing understanding over convenience
- Structured store (preferences, hard facts): plain SQLite table — don't overthink this piece

**[OPEN]** Exact retrieval strategy (how many memories to pull per query, how to blend recency vs. similarity, when to write vs. discard a piece of conversation as "worth remembering"). This is genuinely an iterative design problem you'll tune by watching Ember behave, not something to fully solve on paper first.

**Key mechanical point to hold onto:** memory retrieval happens **before** the LLM call, as part of building the prompt (§2 step 2), and writing happens **after** the reply is generated (§2 step 7). It's not a background process the model is aware of — it's plumbing your orchestrator handles explicitly, every turn.

---

## 4. Voice I/O — wake-word → STT → (orchestrator) → TTS

**[DECIDED]** Text-first for Stage 1. Voice is a Stage 4 concern, currently blocked by unrelated tooling limits per your original doc — but the pipeline is worth having mapped now so Stage 1 code doesn't need rearchitecting later.

**Pipeline:**
```text
Mic → [always-on, near-zero cost] Wake-word detector → 
[triggered] Whisper STT (whisper.cpp, small/base model) → text →
[same orchestrator loop as text input, §2] → reply text →
[cheap, fast] Piper TTS → speaker
```

**[PROPOSED]:**
- Wake-word: **openWakeWord** (open-source, trainable on a custom "Ember" phrase) or **Porcupine** (easier, some licensing to check for your use case)
- STT: **whisper.cpp**, `small` or `base` model — `large` will be a genuine bottleneck on this CPU, similar in spirit to running an oversized LLM
- TTS: **Piper**, browsing its pre-trained voice list for something matching the "intelligent, composed woman" direction from your original doc — this is choosing a voice model file, not designing a voice from scratch

**Design implication for the orchestrator (§2):** it should be built so voice and text inputs both normalize to the same text-in path, and replies both normalize to the same text-out path (with TTS as an optional final step). Don't build two separate pipelines — build one pipeline with two entry/exit adapters.

**[OPEN]** Whether STT/TTS run concurrently with LLM inference or must wait their turn, given RAM pressure (§6) — a real scheduling question once voice is actually being built, not before.

---

## 5. Tools — structured output + your code executing it

**What it is, mechanically:** the model is given tool descriptions as context, and (if trained for it) outputs a structured JSON request instead of a normal reply when a request matches a tool. Your orchestrator parses this, runs the *real* function, and feeds the result back for a second LLM pass.

**[DECIDED — from original doc, §17]** Any tool with real external effects needs permission/safety gating. This is not optional and not a "later" concern to bolt on — it should exist from the first tool you add, even if the tool is trivial (e.g., "read a file").

**Concrete permission model to consider [PROPOSED]:**
- Tiered tool categories: **read-only** (list files, check weather, query memory) → generally auto-allowed; **local write** (create/edit a file) → confirm by default; **system-affecting** (run a script, install something) → always require explicit confirmation, no exceptions, regardless of how the model phrases its confidence.
- Log every tool call, executed or denied — this becomes your episodic memory (§3) and your debugging trail simultaneously.

**Model dependency:** tool-calling reliability is not universal across models (theory: Section 9 of our discussion) — this is a real factor in your §1 model choice, not a separate concern. If Qwen2.5-7B's tool calling proves unreliable in practice, that's a legitimate reason to swap models, not just prompt-engineer harder.

**[OPEN]** The actual first tool set to build. Suggest starting genuinely small — file read/write in a sandboxed directory, maybe a basic web search — to validate the whole round-trip (§2 step 5) works before adding anything with real consequence.

---

## 6. Resource Manager — Vivobook-first, Tier 2 on demand

**[DECIDED — architecture priority from original doc]** Vivobook is Tier 1, always tried first. New laptop is Tier 2, only engaged when Vivobook genuinely can't handle the load, and shouldn't be treated as Ember's default home.

**What "can't handle the load" concretely means, given what you now know:**
- RAM pressure: model (§1) + memory system (§3, lightweight) + voice models (§4, when active) + OS overhead approaching your 16GB ceiling
- Latency: a task where CPU-bound generation (§1's 3-8 tok/s reality) makes a response impractically slow — e.g., a much larger model needed for a specific hard task
- Concurrent load: user actively using the Vivobook for something else demanding, competing for the same CPU/RAM

**[OPEN, honestly — this is the least-baked part of your whole doc, and that's fine at this stage]** The actual mechanism for Tier 2 escalation. Realistic candidates, roughly in order of complexity:
1. Simplest: Ollama running on *both* machines, orchestrator on the Vivobook calls the new laptop's Ollama API over your local network when escalating — no exotic infrastructure, just an HTTP call to a different host.
2. More involved: a task queue (e.g., something like Redis or even a simple file-based queue) if you want async/retry behavior rather than a direct blocking call.
3. Skip for now: this genuinely doesn't need solving until Stage 6/7 per your own roadmap — the Vivobook alone can carry Stages 1–5.

**Recommendation:** don't design this deeply yet. Note it, keep the orchestrator's LLM client (§1) abstracted enough that "which host to call" is a config value, not hardcoded — that's the only forward-compatibility investment worth making now.

---

## 7. Context Management — keeping the window cheap

**Mechanical recap:** context cost scales roughly quadratically with length (attention, theory §4/§5), and KV cache eats real RAM on top of model weights — both directly relevant on a 16GB machine.

**[PROPOSED] Strategy, from cheapest to most sophisticated, roughly matching your stage progression:**
- Stage 1: naive truncation (drop oldest messages past a token budget) — fine for a bare LLM loop with no memory yet
- Stage 2+ (once §3 memory exists): shift to the RAG model — don't try to keep long history *in* the window at all; write it to memory immediately, retrieve only what's relevant per query. The window becomes short-lived working state, not the memory system.
- Optional refinement: periodic summarization of recent-but-not-immediate conversation, as a middle layer between "just happened" (raw, in-window) and "a while ago" (vector-retrieved).

**Concrete orchestrator responsibility (ties to §2 step 2):** before every LLM call, the orchestrator must actively decide what goes into the prompt and enforce a token budget — this isn't automatic, and it's one of the more important pieces of "judgment" code in the whole system.

---

## 8. Personality & Identity

**[DECIDED]** Name (Ember), voice direction (intelligent, composed woman), general trait direction (intelligent, composed, capable, calm, helpful, consistent).

**Mechanism (theory §9):** this lives almost entirely in the **system prompt**, not a separate "personality module" or fine-tuning. Concretely: a well-written, consistent system prompt defining tone, verbosity, how Ember refers to itself, how it handles uncertainty, etc. — re-sent every single call (§2 step 2), same as any other context.

**[OPEN]** The actual system prompt text, and whether/how it evolves (e.g., does Ember's "familiarity" with you increase over time — and if so, is that personality-layer or actually memory-layer, since "knows you well" is really retrieved facts, not a changed personality). Worth deciding once Stage 2 memory exists — the two will likely blend at the prompt-assembly step (§2 step 2 again).

---

## 9. What Ties This All Together

Every subsystem above talks to the orchestrator (§2), and the orchestrator is the only component that needs to understand all of them at once. This is deliberate: it means you can build and test each piece almost independently —

- §1 (LLM engine) works and is testable with zero memory, zero tools, zero voice
- §3 (memory) can be built and tested with a script that just embeds/stores/retrieves, independent of the orchestrator
- §5 (tools) can be tested by manually crafting a fake "tool call" JSON and checking your executor handles it correctly, without needing the LLM to generate it correctly yet
- §4 (voice) can be tested by feeding Whisper a pre-recorded file, independent of the live orchestrator loop

This independence is the practical payoff of the modularity principle from your original doc (§21.2) — not an abstract nicety, but literally how you'll be able to build and debug this thing one piece at a time without needing the whole system working to test any one part.

---

## 10. Immediate Next Steps (unchanged from the Stage 1 brief, restated for continuity)

1. Install Ollama, pull Qwen2.5-7B-Instruct (Q4_K_M) and Phi-3.5-mini
2. Benchmark both raw via `ollama run` — real tokens/sec on your actual CPU
3. Build `llm_client.py` — thin wrapper around Ollama's local API
4. Build `ember_core.py` — the boring version: input → llm_client → output
5. Once that loop is solid and feels right, layer in a basic system prompt (§8) — cheapest, highest-leverage next step before touching memory or tools

Everything else in this document is context for *why* each later step matters — not a queue to start working through top to bottom.
