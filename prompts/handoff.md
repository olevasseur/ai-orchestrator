
# Book Summarizer — Local Human-in-the-Loop Orchestrator Handoff

## 1. Project overview

### What the book summarizer does
This project is a **local-first book analysis and summarization tool** built around a deterministic RAG pipeline.

It ingests books, detects structure, chunks content, stores embeddings in a local vector store, builds summary windows inside sections, selects windows for summarization, synthesizes section summaries, and then synthesizes a book summary.

The tool is meant to become a **navigable semantic index of books**, supporting:

- section-aware summaries
- inspection of raw chunks and summary windows
- inspection of selected windows and their scoring
- summary quality evaluation
- idea tracing through a book
- eventually cross-book comparison

### Current goals
The immediate goal is **not a large architecture change**.

The summarizer core is now usable. The next step is to make the book **more explorable** by adding a small navigation-oriented capability on top of the existing summarization and evaluation pipeline.

### Current architecture (high level)

Pipeline:

1. **Ingestion**
   - parse PDF/text
   - detect sections (introduction, chapters, etc.)
   - split sections into chunks
   - embed chunks into vector store

2. **Summarization**
   - group chunks into summary windows
   - score windows using multiple signals
   - select windows using diversity-aware selection (MMR)
   - summarize selected windows
   - synthesize section summaries
   - synthesize book summary

3. **Evaluation & inspection**
   - inspect structure
   - inspect chunks
   - inspect windows
   - inspect window selection
   - inspect summary metadata
   - evaluate summary faithfulness

Main modules:

- rag/config.py
- rag/chunker.py
- rag/ingest.py
- rag/store.py
- rag/retrieval.py
- rag/analysis.py
- rag/synthesis.py
- rag/critic.py
- rag/evaluate.py
- rag/inspect_utils.py
- rag_cli.py


---

# 2. Current state

## What has already been implemented

Implemented capabilities:

- deterministic ingestion pipeline
- section detection and normalization
- hierarchical structure (parts → chapters)
- chunking within sections
- local embeddings and vector store
- summary windows inside sections
- multi-signal window scoring
- diversity-aware window selection (MMR)
- quality tiers:
  - fast
  - default
  - thorough
- per-section summarization
- book summarization
- summary metadata output
- selection detail artifact (`selection_detail.json`)
- summary quality evaluation
- caching of window summaries and section summaries
- CLI inspection commands

Inspection commands currently available:

- inspect structure
- inspect chunks
- inspect windows
- inspect selection
- inspect summary-meta

## What has been tested

Tested using the book **Digital Minimalism**.

Validated behaviors:

- section detection
- chunk counts
- window generation
- selected window inspection
- quality tiers
- section summarization
- evaluation outputs
- cache reuse

Example LLM usage:

default: ~54 calls (cold)
fast: ~33 calls
thorough: ~75 calls


## What is working well

Strong areas:

- reliable section detection
- stable chunking
- multi-signal window scoring
- MMR window selection
- inspection tools
- evaluation that identifies missing ideas
- caching significantly reducing rerun cost

## What is incomplete or uncertain

Remaining uncertainties:

- warm reruns still sometimes trigger a few LLM calls
- summary quality is good but not perfect
- navigation layer is weak
- no clean workflow yet for:

  - tracing ideas through the book
  - finding recurring concepts
  - finding examples for an idea
  - zooming from chapter summary to supporting passages


---

# 3. Important decisions already made

## Design choices

- local-first architecture
- deterministic pipeline instead of autonomous agents
- strong inspectability
- separate chunking and summarization layers
- summary windows instead of whole-chapter summarization
- quality tiers
- faithfulness over automation

## Constraints

- do not rewrite architecture
- keep CLI workflow
- keep summarization deterministic
- preserve inspectability
- preserve caching

## Tradeoffs

- speed is secondary to faithfulness
- longer cold runs are acceptable
- generated artifacts remain local
- trustworthiness is more important than aggressive compression

## Things we explicitly do NOT want to change

- section detection logic
- chunking architecture
- quality tier system
- inspect commands
- deterministic summarization pipeline


---

# 4. Recommended next increment

## Next task
Implement a **navigation command** that traces an idea through a single book.

CLI command:

```
python rag_cli.py trace <book_id> --idea "<query>"
```

The command should show:

- matching sections
- selected windows containing the idea
- short previews of window summaries
- page ranges
- where the idea appears in the book


## Why this is the best next step

The summarizer is already usable.

The biggest missing capability is **navigation and exploration**.

This task:

- uses existing artifacts
- does not require architecture changes
- makes the tool immediately more useful
- is small and low risk

## Keep it small

Do NOT implement:

- a concept graph
- cross-book reasoning
- ontology extraction

Only implement **simple single-book idea tracing**.


---

# 5. Implementation guidance

## Files likely to be touched

- rag_cli.py
- rag/inspect_utils.py

Optionally create:

- rag/navigation.py

## Key behaviors to preserve

Preserve:

- summarize commands
- evaluation commands
- inspect commands
- cache behavior
- summary artifact formats

Navigation should **read existing artifacts**, not regenerate summaries.

## Edge cases

Handle:

- books without summaries yet
- ideas that match nothing
- ideas matching many sections
- chapters with no selected windows
- CLI quoting issues


---

# 6. Validation

Run:

```
python rag_cli.py summarize digital-minimalism --quality default
python rag_cli.py trace digital-minimalism --idea "digital minimalism"
python rag_cli.py trace digital-minimalism --idea "solitude"
python rag_cli.py trace digital-minimalism --idea "high-quality leisure"
python rag_cli.py trace digital-minimalism --idea "attention economy"
```

Expected behavior:

The command prints:

- the idea being traced
- matching sections
- page ranges
- preview snippets from window summaries

Output should be readable and grounded in existing summaries.


---

# 7. Risks / caveats

Potential misunderstandings by the coding agent:

- building a knowledge graph instead of a simple trace command
- modifying summarization architecture unnecessarily
- triggering new LLM calls instead of using existing summaries
- searching only raw chunks instead of summaries

Human review should verify:

- usefulness of trace results
- clarity of preview snippets
- whether search should include section summaries and window summaries


---

# 8. Task for local orchestrator

Implement a new CLI command to trace an idea through a summarized book.

Command:

```
python rag_cli.py trace <book_id> --idea "<query>"
```

Example:

```
python rag_cli.py trace digital-minimalism --idea "solitude"
```

Requirements:

1. Reuse existing summary artifacts.
2. Search section summaries and selected-window summaries.
3. Output:

   - matching sections
   - section titles
   - page ranges
   - short preview snippets

4. Keep implementation simple and deterministic.
5. Do not modify ingestion, chunking, or summarization architecture.

Validation:

```
python rag_cli.py summarize digital-minimalism --quality default
python rag_cli.py trace digital-minimalism --idea "solitude"
```

Success criteria:

- command runs successfully
- results are readable
- previews correspond to real summary artifacts
- no regressions in existing commands
