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
