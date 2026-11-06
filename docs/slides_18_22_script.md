# Speaker Script — Slides 18–22 (Target: 7 minutes)

**Timing budget:** 18 (0:70) → 19 (0:85) → 20 (0:60) → 21 (1:40) → 22 (1:15) = ~6:30, leaving ~30s buffer for pacing/questions.

---

## Slide 18 — PIR in Real-Time Applications
**Target: ~70 seconds**

So far we've looked at PIR schemes purely as cryptographic primitives — how fast can we do one retrieval. Before we get to our comparison, I want to zoom out and ask: where does PIR actually get used when latency matters in the real world?

There are four examples worth knowing. First, **metadata-private messaging** — Addra hides who's talking to whom using PIR-retrieved mailboxes, so message latency is directly bounded by PIR retrieval time. Second, **private media streaming** — Popcorn applies PIR to a Netflix-scale catalog, batching many users' requests together to amortize server cost. Third, **private contact discovery** — apps like Signal need to resolve large contact databases privately, and users expect that in under a second. And fourth, **low-latency lookups** — things like private DNS or private search, where people expect the same speed as the non-private version.

The takeaway that ties all of these together: PIR's real-world viability isn't decided by how strong the cryptography is — it's decided by whether the server's computation and communication actually fit inside the application's latency budget. That's the lens we'll use for the rest of this section.

*(Transition: "Let's go deeper on one of these, because it's the closest real system to a real-time PIR case study.")*

---

## Slide 19 — Addra / FastPIR
**Target: ~85 seconds**

The problem Addra tackles: existing PIR schemes can be too slow and bandwidth-heavy for latency-sensitive apps like voice calls. Addra's solution is to have callers and callees exchange messages through private mailboxes on an untrusted server, retrieved via PIR — so the server can't tell who's talking to whom.

To hit real-time latency, they built **FastPIR** — a computational PIR scheme that specifically cuts server-side answer-processing time by avoiding the recursive query techniques that schemes like SealPIR and OnionPIR use. That trade-off costs some communication efficiency, but buys speed.

And the results back it up: Addra scales to **32,000 users** with **726 milliseconds** 99th-percentile latency on an 80-machine cluster — a **7x improvement** over the prior system. The cost: about **1.46 Mbps** of bandwidth per user, and a 96-byte voice message balloons into a 64-kilobyte ciphertext — roughly **682x** larger.

The real lesson for us: Addra shows real-time PIR is achievable, but only with an application-specific scheme and serious infrastructure parallelism. It's not a free lunch.

*(Transition: "Now, before I show you the comparison table, let me set up how we built it — because the axes matter as much as the numbers.")*

---

## Slide 20 — What Should We Compare?
**Target: ~60 seconds**

We're comparing every system we surveyed along six dimensions:

- **Communication cost** — how much data moves per retrieval
- **Computation cost** — server and client processing time
- **Batch support** — can it retrieve several chunks more cheaply than one at a time
- **Online/offline split** — how much work can be precomputed before the query even arrives
- **Threat model** — information-theoretic multi-server, or computational single-server
- **Real-time fit** — does the online latency actually fit inside a playback buffer's deadline

These six axes are exactly what feed into the table on the next slide.

*(Transition: "So here's the full comparison, ten systems side by side.")*

---

## Slide 21 — Literature Comparison
**Target: ~100 seconds**

A few patterns jump out. Look at the early systems — **XPIR** is poor across almost every column; it proved single-server PIR was possible but wasn't built for speed. Then **SealPIR** and **OnionPIR** improve communication — one attacks the query side, one attacks the response side — but neither directly targets computation.

Then there's the computation-focused generation — **SimplePIR, SPIRAL, FrodoPIR, and Piano** — these chase low online latency, and they all land in the "good" real-time-fit column.

**Vectorized BatchPIR** is the only scheme in this table natively built for batch retrieval — which matters a lot for us, since video playback is inherently a batch-retrieval problem.

And then the two outliers at the bottom — **Popcorn** and **Addra** — these aren't just schemes, they're full deployed systems. They're the only two rows here that have actually been evaluated end-to-end against a real application deadline, rather than just benchmarked in isolation.

One caveat worth saying out loud: these ratings are qualitative. The papers benchmark under different database sizes and hardware, so this isn't a strictly apples-to-apples numeric comparison — it's a relative positioning.

*(Transition: "Let's zoom out one more time and look at this as a timeline — I think it tells the story of the whole field in about fifteen seconds.")*

---

## Slide 22 — Evolution of the Literature
**Target: ~75 seconds**

**2016:** XPIR proves single-server PIR is practical at all. In that same window, Popcorn and the convolutional-codes streaming work show PIR can be applied to continuous video and media consumption specifically.

**2018–2021:** SealPIR and OnionPIR chip away at communication cost.

**2020–2024:** SimplePIR, SPIRAL, FrodoPIR, and Piano chip away at computation cost.

**2021–2023:** Addra's FastPIR and Vectorized BatchPIR both push toward real-time and batched retrieval specifically.

So — fifteen years of research have built every individual piece we need: fast single-server retrieval, efficient batching, and real-time-capable schemes. What nobody has done is combine all three at once, for video, under a real playback deadline. That's exactly the gap our project sits in — which is what our Research Gap section walks through next.

---

## Delivery notes
- **If you're running long:** trim Slide 21's per-row commentary down to just the three group patterns (early/communication-era/computation-era) and skip the individual XPIR/SealPIR/OnionPIR call-outs — saves ~20s.
- **If you're running short:** expand the Addra numbers on Slide 19 with a one-line comparison to a "normal" (non-private) messaging system's latency, if you have that figure handy, or add a sentence on why 682x ciphertext expansion matters for bandwidth-constrained users.
- **Likely audience question:** "Why is everything in the table 'Computational' type?" — Answer: this table only includes single-server computational PIR schemes (that's this project's target setting); the multi-server information-theoretic scheme from the streaming literature is covered separately since it doesn't fit a single-server deployment.
