# Cambridge Battlecode 2026a: strategy notes

Competition: https://2026a.battlecode.co.uk (handbook at /game/docs; full rules
export via the "Agents.md" page -> GameRules.md).

## CORE PRINCIPLE (do not lose this)

**Fuel is a rule, not a constraint.**

> Everything our submission can affect is an action; everything that limits
> it is a rule of the game. Nothing sits outside the game as a "constraint on
> the policy."

| Piece | Action | Rule |
| --- | --- | --- |
| Compute | spending fuel (instructions, SDK calls) | per-turn fuel budget, suspension on exhaustion |
| Champion select | each pick | uniqueness, simultaneous reveal, random pick on failure |
| Shared memory | each read/write | 8 bytes; turn order decides who sees a write when |
| Draft memory | one-shot broadcast from the full-map draft | 1,024 bytes, read-only after draft |
| Turn order | `set_priority` | team alternation, takes effect next round |
| Other | yield, resign, language choice, even crashing | their engine semantics |

Communication is not designed separately from play. What to write, how to
encode it and when are part of the strategy, optimised jointly with
everything else. All instances run the same program, so the codebook is
shared for free: the team is one strategy split across bodies, linked by an
8-byte channel plus implicit signals (positions, timing, observed behaviour).
"Design a protocol, then a policy" is the same mistake as "train a policy,
then fit it to fuel."

### End-to-end plan (current, supersedes details below where they differ)

- **Single output: one policy.** It covers the draft bot and every champion
  (branching on champion type). The memory scheme is not a separate output:
  writes are policy outputs, reads are policy inputs, and the meaning of the
  bytes is whatever the shared program makes it.
- **A policy is a program composed of building blocks**, e.g. `2x + y*z`
  with blocks as variables. Its configuration = which blocks, how they are
  combined (structure, conditions), and the constants. Blocks are compute
  moves with a fuel price at a fixed setting, e.g. "A* depth 2" (goal and
  scoring fixed; only tool + depth chosen), read/write shared bytes, read
  remaining fuel, visible units, fight outcome, lookahead depth k, move,
  attack/cast.
- **No hard fuel constraint on the policy.** The simulator applies the real
  rule (overrun = suspend, lose tempo); "read remaining fuel" is a block, so
  wasteful policies just lose games.
- **Generator: Deep Symbolic Regression style** (Petersen et al., ICLR 2021).
  An offline network writes policies token by token; invalid tokens are masked
  by the policy grammar; constants are tuned in an inner loop; reward = win
  rate in our simulator; risk-seeking policy gradient (train on the top few %
  of each batch). No model runs on the bots.
- **One policy for all maps, adapting at runtime.** Tournament maps are
  unseen, so no per-map policies offline. Terrain is not discovered: the
  draft sees the whole map and champions can read walls/altars/camps via
  `game_map`; only units are discovered through vision. Map-tailoring happens
  inside the policy: branch on map blocks, and/or the draft picks a
  sub-policy and writes the choice into draft memory.
- **Fuel prices must be reliable**: each block's price is a function of
  inputs (map size, unit count, depth), fitted by measuring against `cambc`.
- **Blocks are split to the smallest manageable unit**, so the policy pays
  only for what it uses. E.g. map reading = separate blocks: walls, holes,
  bushes, altars, spawn regions, camp groups, symmetry. All are readable from
  turn one (and in the draft); live camp state and enemies need vision.
- Open: final building-block list; safety margin on fuel estimates.

### Deployment shape (agreed so far)

- No model runs on the bots. The network lives offline and outputs
  **configurations** (data, not code). The bot is a fixed interpreter of a
  configuration.
- Configuration expressiveness is a spectrum (pure numbers ... structure as
  data). Decisions: weighted rules (weights change effective rule order,
  so structure stays differentiable).
- **Storing/reading must be configured, not hand-designed**, so the bot can
  reach the optimal memory use. Bot = memory' = f(memory, obs),
  action = g(memory, obs), with f set by the configuration: what to store, how
  to encode it, when to write, what to read. Matters most for the 8 shared
  bytes and the 1,024 draft bytes.
- **Compute is configured too.** Configuration = compute schedule + weights +
  codebooks. The schedule is an ordered list of compute steps (features at a
  chosen cost/precision tier, action scoring, lookahead), each with a cheap
  gate and an early-stop margin; plus cache/recompute policy and deliberate
  multi-turn computation. The fixed interpreter only walks the schedule and
  must stay negligibly cheap: any real compute left in the fixed part is
  compute outside the game.
- **Building blocks**: the interpreter ships a library of parameterised
  algorithms; the configuration chooses which run, when and with what
  settings (each run is a priced step in the compute schedule). Candidates:
  search/lookahead (depth, beam, move ordering, leaf eval, opponent model,
  fuel cap, anytime; depth 0 = plain scoring), pathfinding (engine distance
  fields or cheap estimates, weighted tile costs), fight calculator
  (closed-form, deterministic), belief tracker (where unseen enemies can be),
  timers (camp respawns, enemy cooldowns/deaths), task assignment via
  shared-memory claims, codebooks, draft map analysis. Start with search,
  pathfinding, fight calculator, belief tracker. The library is now the
  expressiveness ceiling.
- **Why the compute space is manageable.** Raw space = every program within
  fuel (unbounded). But a computation matters only through its outputs (board
  actions, memory writes, whether it overruns). Programs with the same outputs
  are the same move: hash-mining within budget == doing nothing (unused fuel
  doesn't carry over); over budget == skipping a turn. So we choose a
  *decision function* (info -> outputs); compute only decides which decision
  functions are reachable. Per turn, fuel is a knapsack: spend it on
  computations whose information changes decisions (rational metareasoning,
  Russell & Wefald). Keep only the cheapest computation at each precision
  level for each decision-relevant quantity. The block library is this
  pruning. Grow it from evidence: run an unlimited-fuel version of the bot in our
  simulator, find costly decision differences, add the block that closes the
  gap.
- Train encodings DIAL-style (Foerster et al. 2016): relax messages to
  continuous values through a noisy channel during training, discretise at
  execution. The learned encoding ships as lookup tables.

**Compute is part of the action space.**

The rules describe a game of moves. What we actually submit is a program
running under a per-turn fuel budget, so the real game is the *expanded* game:

- State = board state + every bot instance's machine state (its persistent
  memory and where it is in its program) + 8-byte team shared memory +
  1,024-byte draft memory.
- The atomic actions are instructions. Game actions are just the side effects
  of certain SDK calls. API queries (vision, masks, distance fields) cost fuel,
  so what a champion *knows* is also something it chooses to spend on.
- Running out of fuel suspends the bot and resumes it next round, so thinking
  trades directly against tempo (lost turns). Shared-memory writes are actions.
  The draft is the one big-compute, full-information action.

Analogy: a knight moving two forward and one sideways is part of chess's
action space; nobody treats the L-shape as an outside constraint on the policy.
The fuel limit is the same kind of rule, just with more nuance. An action is a
pair (x compute, x board action), and the budget is a legality rule on that
pair, exactly like the knight's move pattern.

Consequence: if we model the expanded game correctly and optimise in it, the
optimum is *by construction* a program that runs within budget. There is no
separate "train an unrestricted policy, then compress/distil it to fit fuel"
step. Any such fitting layer is a sign the game model is incomplete.

Requirements for this to hold:
- Fuel costs in our model must match the real engine exactly (measure them
  against the `cambc` binary).
- Bot memory must be part of the state.
- Code/module size must be a constraint too: fuel alone does not stop the
  optimiser from producing a huge zero-fuel lookup table. Submission size limit
  is undocumented; still to be determined.

## Plan so far

- Policy is trained/optimised offline and hardcoded into the bot, with a little
  room for runtime computation inside the policy. No network is loaded onto the
  champion.
- Gradient ascent is over continuous parameters of a stochastic policy
  (expected score is smooth in action probabilities; discreteness of the game
  itself is not the obstacle). Two-player, so the target is an equilibrium.
- Write our own fast simulator in a low-level language. The `cambc` engine
  (closed ~99 MB binary, pip package `cambc`) is deterministic and serves as
  the reference: differential-test our simulator against it round by round.

## Facts that constrain the design

- One bot instance per champion (Cassi & Issac = two), plus a draft instance.
  Fuel is per instance per turn: 100k (C++/Rust), 5x OCaml, 25x Python; unused
  fuel does not carry over; draft's first turn 10x, first play turn 2x.
- Instances share only 8 bytes (read/write all match) and 1,024 bytes of draft
  memory (read-only after draft). Vision is not shared.
- No randomness in play; the only randomness is auto-picks for a failed draft.
- `debug_block` is fuel-free only in local debug builds; measure fuel with
  `--release`.
- Sprint tournaments use only unseen maps, so per-map hardcoding is out:
  map-specific planning belongs in the draft.
- Rule details still ambiguous (resolve against the binary): monster target
  tie-breaks, damage modifier ordering, dash path legality, vision edge cases.
