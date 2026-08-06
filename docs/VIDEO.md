# Demo video — shot list

**Target: 2:45.** Judges are not required to watch past three minutes, so the thesis and the
proof both have to land in the first ninety seconds.

Terminal at ~110 columns, dark background, font large enough to read at 720p. Run
`faultline demo` once before recording so images and imports are warm.

---

## 0:00–0:18 — The hook

**Screen:** black, then a single line of text.

> A churn model went to production at 0.94 AUC.
> Six weeks later it was costing $180K a quarter.
> It never drifted.

**Voiceover:**

> Every drift monitor watches the data. Nobody watches the shape of the pipeline — and that
> is where models actually die.

---

## 0:18–0:40 — Why nothing caught it

**Screen:** `python demo/measure_skew.py`, output already on screen.

Highlight two numbers as they are spoken.

**Voiceover:**

> This is a real train/serve skew: the training feature averages over thirty days, the
> serving feature over seven. Compare the distributions and they differ by **one point four
> percent**. No monitor alerts on that. But **half the rows** the model is actually served
> are more than ten percent wrong. The defect is invisible in the data — and unambiguous in
> the lineage graph.

---

## 0:40–1:20 — Faultline finds it

**Screen:** `faultline demo --explain`

Let the pipeline summary render, then the findings table.

**Voiceover:**

> Faultline reads DataHub's metadata graph. Ten tables, forty-three column-level lineage
> edges — parsed out of the compiled SQL by DataHub's own parser, not declared by hand.
> Four ordinary changes were merged into this pipeline. Faultline reports exactly four
> findings, each at its root cause.

Scroll to the **leakage** panel. Hold on the proof path.

> This one is the showstopper. A segment churn-rate rollup — a perfectly reasonable
> dashboard metric — is computed from the churn label, and then picked up as a model
> feature. Faultline doesn't infer that. It shows you the path: the label, to the rollup, to
> the offline feature column, to the feature the model consumes. Three hops, and every edge
> exists in DataHub.
>
> That 0.94 AUC was never real.

> **Check against the screen before recording:** the panel title says `(3 hops)` and the
> proof block says `3 lineage hop(s)`. Say the number the screen shows.

---

## 1:20–1:45 — It names the line to fix

**Screen:** the train/serve skew panel.

> And here is the skew, with both derivations quoted from the compiled SQL. Minus thirty
> offline. Minus seven online. It doesn't just say the paths diverged — it tells you which
> line to change.

---

## 1:45–2:10 — The agent

**Screen:** `faultline triage --demo`

> Claude reads the proven findings, and reads DataHub itself through its own MCP server. It
> explains consequence, and it can move a severity by one step in either direction with a
> stated reason. Here it took PII *down* — only the email domain survives the transform —
> and took the train/serve skew *up*, because a four-times window mismatch on one of five
> inputs corrupts every prediction, not a subset.
>
> What it cannot do is invent a finding. Every structural claim comes from deterministic
> traversal. The model does the judgement; the graph does the proof.

**Screen note:** the run ends with the cost line — `4/4 assessed · … ~$0.44`. It is worth a
beat: this is the whole triage, on a live catalogue, for under fifty cents.

---

## 2:10–2:35 — It contributes back

**Screen:** `faultline writeback --apply`, then cut to the DataHub UI on the model page.

Show, in order: the `Faultline_TargetLeakage` tag, the `io.faultline.risk` structured
property, the open incident with the proof path in its body.

> Findings go back into the graph — tags, structured properties, an incident carrying the
> proof, and an audit record of the scan itself. The next engineer who opens this model, and
> the next agent that queries it over MCP, inherit the finding instead of rediscovering it.

---

## 2:35–2:45 — Close

**Screen:** the PR comment in GitHub, red, blocking the merge.

> In CI it blocks the pull request before the change reaches the model.
>
> Your model didn't drift. Your graph did.

**End card:** repo URL · Apache 2.0 · `faultline demo` — ten seconds, no setup.

---

## Recording notes

- **Do not** speed-ramp the terminal. The scan genuinely takes under a second; letting it run
  at real speed is the point.
- The `--explain` header states `lineage parsed from compiled SQL` — make sure it is legible;
  it is the claim that separates this from a mock.
- For the DataHub UI section, `demo/verify_live.py` leaves the instance in exactly the state
  the shot needs. Run it immediately before recording.
- No music. Terminal output and a voice is enough, and it keeps the file clean of any
  third-party licensing question.
