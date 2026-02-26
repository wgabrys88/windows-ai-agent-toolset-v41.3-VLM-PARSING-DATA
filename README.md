```
COMPARE FILES WITH THE MOLOSSUS VERSION, I MADE A QUICK FIXES TO IT AND NOT SURE IF THEY ARE INCLUDED HERE
THIS IS FAST CHECK, THERE WERE ONE LINERS CHANGES BUT SUPERIMPORTANT
MAKE SURE TO MERGE THEM INTO THIS REPO - IN OVERALL - THIS REPO IS COOKING
THE PIPELINE.PY is now the ultimate file where we change something,
no more changes across whole system.
```
# AI-Self-Parsing Pipeline (`pipeline.py`)

This repo routes a Vision-Language Model (VLM) through a **pipeline** that produces a stable, app-level format:

- a single **next-turn narrative** (`next_turn`) used as the only text memory fed back into the VLM
- **regions** (UI bboxes) used for overlays
- **actions** used for physical execution
- a **raw_display** object shown in the web panel

The important point: **`pipeline.py` is the only place where “whatever the model returned” becomes “what the app will trust.”**  
You can replace the parsing strategy (JSON parsing, regex, XML, multiple LLM calls, etc.) without changing the rest of the repo, as long as you keep the same output contract.

---

## The contract: `process(raw: str) -> PipelineResult`

`franz.py` imports `pipeline` and calls:

- `result = pipeline.process(vlm_raw)`

and then uses these fields:

- `result.next_turn` → text input to the next VLM request (the “memory”)
- `result.actions` → executed on the desktop
- `result.ghosts` / `result.heat` → drawn as overlays (ghost bboxes + heat trail)
- `result.raw_display` → served to the panel via `/state` and rendered as “VLM Output”

So `pipeline.py` can do anything internally, as long as it returns:

```python
PipelineResult(
  ghosts=[{"bbox_2d":[...], "label":"..."}],
  actions=[{"type":"click", "bbox_2d":[...], "params":"..."}],
  heat=[...],
  next_turn="...",
  raw_display={"observation":"...", "regions":[...], "actions":[...]}
)
```

**No other file needs to change** if you preserve this shape.

---

## Before vs After

### Before (strict JSON parsing)

The VLM was expected to return valid JSON **every turn**:

```json
{
  "observation": "...",
  "regions": [...],
  "actions": [...]
}
```

`pipeline.py` did:

1) `json.loads(raw)`  
2) extract `observation`, `regions`, `actions`  
3) (optionally) summarize the observation  
4) build overlays + output

If parsing failed (bad JSON, fenced JSON, XML), the pipeline fell back to “just treat raw as observation” and lost structure.

**Flow (before)**

```text
 VLM output (must be JSON)
          |
          v
   pipeline.json.loads(raw)
          |
          +--> obs/regions/actions
          |        |
          |        +--> clamp + heat + display
          v
   result.next_turn = obs
          |
          v
 next VLM request text input
```

### After (self-parsing via 3 specialized LLM calls)

The pipeline now treats the VLM output as **untrusted free-form text** (could be JSON, fenced JSON, XML, etc.).
Instead of `json.loads(raw)`, it makes three sequential “self-parsing” calls to an OpenAI-compatible endpoint:

1) **Summarize/normalize observation** → plain text
2) **Extract regions** → JSON list of `{bbox_2d,label}`
3) **Extract actions** → JSON list of `{type,bbox_2d,params}`

The pipeline then clamps bboxes, builds heat, and returns `PipelineResult` as usual.

**Flow (after)**

```text
 VLM output (ANY format: JSON / fenced JSON / XML / text)
          |
          v
   pipeline stage 1: summarize(raw)  ---> plain text observation
          |
          v
   pipeline stage 2: extract_regions(raw) ---> JSON list -> parse+clamp -> ghosts
          |
          v
   pipeline stage 3: extract_actions(raw) ---> JSON list -> parse+clamp -> actions
          |
          v
   heat = build_heat(actions)
   raw_display = {observation, regions, actions}
          |
          +--> panel shows raw_display
          |
          +--> franz executes actions
          |
          +--> next VLM input text = next_turn (the summarized observation)
```

---

## What changed in `pipeline.py`

### Old core idea
- “The model outputs correct JSON, the pipeline parses it.”

### New core idea
- “The model can output anything; the pipeline uses *the model itself* (via 3 specialized calls) to produce a stable app format.”

Concretely, `pipeline.py` now contains:

- **One HTTP client** (`http.client`) targeting `api_url` from `config.json`
- **Three system prompts** (one per stage)
- **Three calls** per turn (sequential, not parallel)
- **Small defensive helpers**:
  - optional unfencing for ```json blocks
  - bbox clamping to 0–1000

The rest of the repo continues to consume the same `PipelineResult` API.

---

## The 3 “parsing” stages (the modular part)

In `pipeline.py` you’ll see three prompts/constants (names may vary):

- `_SUM_SYS`  → summarizes / normalizes the narrative (plain text)
- `_REG_SYS`  → returns ONLY JSON list of regions
- `_ACT_SYS`  → returns ONLY JSON list of actions

Each stage is just:

```text
raw_text  --(system prompt + call /v1/chat/completions)-->  output_text
```

That means you can change the pipeline behavior by editing only:

- the system prompts
- the post-processing (`json.loads`, `clamp`, fallbacks)
- or even replacing a stage with non-LLM code

### Example: tighten region extraction to your UI
Replace `_REG_SYS` with something more domain-specific:

```text
Extract ONLY clickable UI elements in the screenshot:
- buttons, links, tabs, icons
Return ONLY JSON: [{bbox_2d:[x1,y1,x2,y2], label:"..."}]
Coordinates are normalized 0..1000.
```

### Example: restrict allowed action types
If your executor supports only some action types, encode that in `_ACT_SYS`:

```text
Return ONLY JSON actions using these types:
click, double_click, right_click, scroll_up, scroll_down, type, hotkey
Never output drag_*.
```

### Example: replace one stage with traditional parsing
You can keep AI summary but do deterministic parsing for actions:

```text
Stage 1: AI summary(raw) -> next_turn
Stage 2: AI regions(raw) -> ghosts
Stage 3: regex/heuristics -> actions
```

As long as `process()` returns the same `PipelineResult`, everything else works unchanged.

---

## Where the results go

### What the panel shows
The panel renders `raw_display` (typically `{"observation","regions","actions"}`).
So if Stage 1 summary becomes the observation, the panel will display the summarized narrative.

### What the next VLM call receives
`franz.py` uses `result.next_turn` as the next turn’s “previous observation narrative”.
So Stage 1 summary becomes the *actual* memory text used by the agent.

### What gets executed
Only `result.actions` are executed. That’s why Stage 3 matters: if it returns an empty list, nothing happens physically.

---

## Extension ideas (still pipeline-only)

Because the contract is stable, you can add more stages without touching other files:

- **Add a “policy/safety” stage** that deletes unsafe actions
- **Add a “dedupe/merge” stage** that merges overlapping bboxes
- **Add a “planner” stage** that injects a short goal line into `next_turn`
- **Add a “confidence” stage** and include it in `raw_display` for debugging

If you add debug fields to `raw_display` (e.g. `raw_display["_trace"] = {...}`), the server will serve them; the panel can be updated to display them, but nothing breaks if it ignores them.

---

## TL;DR

- `pipeline.py` is a plug-in parsing boundary.
- The rest of the system only cares about `PipelineResult` fields.
- The current pipeline uses **3 sequential LLM calls** to turn arbitrary model output into:
  - a clean narrative memory (`next_turn`)
  - regions
  - actions
- To customize behavior, you usually only edit the three prompts / stage functions in `pipeline.py`.
