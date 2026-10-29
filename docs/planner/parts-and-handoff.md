# Big cards: parts, handoff notes, and context resets

A design to review before any code. It covers three changes: splitting a big card into parts, a
handoff note between parts, and resetting a run's context instead of letting it fill.

## Why

One card is one packet and one coding run, whatever its size. Cards scored complexity 1–2 passed
the checks in 31 of 37 runs; complexity 3 in 25 of 38. Most of what you trashed was multi-file,
multi-repo work. The tutorials card touched 21 files in 2 repos, took 9 rounds and ~1,070 model
calls, and was trashed. Nothing lets a run hand what it learned to the next one, and a long run only
hears "wrap up" at 80% of a window the local model has stopped using well long before then.

## Decisions

### 1. Parts

- **The planner splits; the host enforces it.** A packet of complexity ≥ 3, or one whose files span
  more than one repository, comes back from `packet.problems()` with "split it", the same way a
  hallucinated path does. One planner run has already read the code, over a separate splitter
  agent that would have to read it again.
- **A split packet has `parts`.** Each part is a small packet: title, goal, at most 3 acceptance
  criteria, files, one repository, complexity ≤ 2. At most 6 parts. The parent keeps the goal,
  current behavior and decisions, and the decisions apply to every part.
- **Parts are ordinary cards.** Each is a markdown file with `parent: <id>` and `after: <id>` in
  its front matter, the way `branches:` is today. There is no new storage, and `mv` still fixes
  anything.
- **Order is a chain, not a graph.** `after:` names one card. The daemon runs one card at a time
  anyway, so a graph would buy nothing yet.
- **You approve the breakdown once.** Moving the parent to `todo` moves all its parts there. The
  daemon codes a part only when its `after:` card has passed review, and never codes the parent
  itself.
- **One branch, one PR.** Every part lands on `rfa/<parent-id>`, and each starts from the commit the
  part before it landed. This is the path fix rounds already take (`land(..., onto=True)`).
- **Review per part, app drive once.** Each part gets the code review. The app is driven only after
  the last part, because the app may not boot halfway through a chain. Your verdict and the Create
  PR button are on the parent.
- **A failed part stops the chain.** When a part runs out of attempts, the parent shows `blocked`
  and names the part. You can edit that part and retry it, or plan the remaining parts again from
  the branch as it stands. Over carrying on: a later part built on a broken one is wasted work.

### 2. Handoff notes

- **The host owns the note.** `var/runs/<parent>/progress.md` is appended by the host from each
  part's `summary.json`, which gains a `notes` field: what was decided, what was surprising, what the
  next part should know. Over letting the coder edit a file in the repository: the diff stays clean,
  and a model cannot rewrite what earlier parts wrote.
- **Each part is told where it stands.** The coder prompt gets an "Earlier parts" section:
  - the list of parts, marked done, current, or later;
  - the notes;
  - `git log --stat` for the parent branch since its base.

  The section is capped at about 4k tokens, and the newest notes win.
- **Replanning reads the note too.** When you plan the remaining parts again, the planner starts
  from what was built, not only from the idea.

### 4. Context resets inside a run

- **A run resets before its window fills.** At `reset_at` (default 0.6 of the window), the host
  asks the coder for one turn to write `/work/progress.json`: what is done, what is left, which
  check is failing, which files it touched. The messages then reset to:
  - the system message;
  - the packet;
  - "Where you left off": that note, `git diff --stat`, and the last check output.
- **The limits stay.** At most 2 resets. After that the existing 80% wrap-up and auto-submit apply.
  The step budget keeps counting from the first edit; a reset does not refill it.
- **Resets use the handoff format.** The note has the same shape as part 2's, so one renderer
  serves both. A reset is a handoff from a run to itself.
- **The coder writes its own summary.** It knows what matters, so its note beats a separate model
  call summarising the transcript, and it beats truncating old observations, which is cheaper but
  throws away the reasoning.
- **Nothing is lost from the record.** The trajectory keeps every segment, so the board still shows
  the whole run.

## Questions for you

1. **Tests for parts.** A later part's tests cannot be written up front against code that does not
   exist yet. Default: each part's tests are written automatically when its turn comes, without
   your approval. The alternative is that you approve each part's tests before it codes.
2. **When to split.** Default: complexity ≥ 3 or more than one repository. Should a file-count rule
   apply too, such as more than 8 files?
3. **Approval.** Default: one approval for the whole breakdown. Or approve each part?

## Where it lands

| Change | Files |
| --- | --- |
| `parts` on the packet, the split rule | `packet.py`, `config/planner.yaml` |
| `parent` / `after`, `blocked`, moving parts with the parent | `tasks.py` |
| Skip parents and parts that are not unblocked | `daemon.next_job` |
| Start from and land on the parent branch, append the note | `worker.run_task` |
| "Earlier parts" section, `notes` in the summary | `config/coder.yaml` |
| Reset at `reset_at` | `worker.CoderAgent` |
| Drive the app on the last part only | `reviewer.py` |
| Parts grouped under their parent | `board.py`, `board.html` |

## Build order

1. **Context resets (4).** Self-contained, and it helps every card at once.
2. **Parts with handoff notes (1 + 2), together.** Parts without notes start blind, which is the
   failure we already have.

## Not in scope

- Running parts in parallel, or ordering them as a graph.
- Picking the model by complexity (idea 3), which is separate and smaller.
