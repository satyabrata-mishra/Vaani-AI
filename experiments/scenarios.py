"""Load scenarios. Rates are chosen against the measured capacity: ~8,000 tokens/min at
~1,650 tokens per full answer = ~4-5 answers/min (R3 + Phase 5 smoke run).

Patience (people giving up before the 30 s deadline) is an ASSUMPTION, not a measurement:
by default 20 % of people give up after 8-15 s, the rest wait for the deadline."""
from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Patience:
    impatient_share: float = 0.2
    impatient_s: tuple[float, float] = (8.0, 15.0)
    patient_s: float = 120.0  # longer than any deadline: these people wait for the system to answer

    def draw(self, rng: random.Random) -> float:
        if rng.random() < self.impatient_share:
            return rng.uniform(*self.impatient_s)
        return self.patient_s


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    profile: str = "normal"                 # stand-in profile at the start
    rate_per_min: float = 4.0               # background arrival rate
    duration_s: float = 300.0               # arrivals stop after this
    arrival: str = "poisson"                # poisson | constant
    burst_size: int = 0                     # extra requests arriving at once ...
    burst_at_s: float = 0.0                 # ... at this offset
    switch_to: str | None = None            # stand-in profile switched to mid-run, without telling the client
    switch_at_s: float = 0.0
    deadline_s: float = 30.0
    retrieval_setting: str = "medium_k4_rr"
    patience: Patience = field(default_factory=Patience)
    retry: str = "policy"                   # policy | none | naive (RetryPolicy(naive=True))

    def arrivals(self, rng: random.Random) -> list[float]:
        times: list[float] = []
        if self.rate_per_min > 0:
            gap = 60.0 / self.rate_per_min
            t = rng.expovariate(1 / gap) if self.arrival == "poisson" else 0.0
            while t < self.duration_s:
                times.append(t)
                t += rng.expovariate(1 / gap) if self.arrival == "poisson" else gap
        times += [self.burst_at_s] * self.burst_size
        return sorted(times)

    def to_dict(self) -> dict:
        return asdict(self)


SCENARIOS: dict[str, Scenario] = {s.name: s for s in [
    Scenario("steady", "Below capacity: ~4 questions/min for 10 min.",
             rate_per_min=4, duration_s=600),
    Scenario("burst", "Quiet background (2/min) plus 20 questions arriving at the same moment.",
             rate_per_min=2, duration_s=240, burst_size=20, burst_at_s=60),
    # 20/min: ~4x full-answer capacity and still ~2.5x the degraded capacity (~1,040 tokens/answer).
    # A first run at 12/min was absorbed by degraded mode (37/50 degraded, 0 turned away).
    Scenario("overload", "Sustained overload: 20 questions/min for 5 min.",
             rate_per_min=20, duration_s=300),
    # Constant 5/min: just above full capacity before the switch, ~1.9x the halved capacity after it.
    # A first Poisson 4/min run realised only ~2.7/min after the switch and barely reached the new limit.
    Scenario("limit_halved", "5/min for 8 min; after 4 min the provider halves RPM and TPM without notice.",
             rate_per_min=5, duration_s=480, arrival="constant", switch_to="rate_limited", switch_at_s=240),
    Scenario("daily_exhausted", "6/min for 5 min with only 20 requests left in the provider's daily budget.",
             profile="daily_exhausted", rate_per_min=6, duration_s=300),
]}

# Does retrying help or hurt? Same seeded arrivals against a stand-in that fails 20 % of calls (an
# assumed rate: no 5xx was seen on Groq), once with spare capacity and once busy. A retried call
# competes for the same per-minute token budget as new questions. The first, 4/min set realised
# 3.2/min with a peak queue of 1, so retries had nothing to compete with.
RETRY_SCENARIOS: dict[str, Scenario] = {s.name: s for s in [
    *[Scenario(f"errors_{mode}", f"4/min for 6 min, 20 % of model calls fail; retries: {mode}.",
               profile="erroring", rate_per_min=4, duration_s=360, retry=mode)
      for mode in ("policy", "none", "naive")],
    *[Scenario(f"errors_busy_{mode}", f"12/min for 5 min, 20 % of model calls fail; retries: {mode}.",
               profile="erroring", rate_per_min=12, duration_s=300, retry=mode)
      for mode in ("policy", "none", "naive")],
]}

# Small real-Groq baseline (needs --backend real --yes; budget printed by --dry-run)
REAL_SCENARIOS: dict[str, Scenario] = {s.name: s for s in [
    Scenario("steady_real", "Real Groq baseline: 3 questions/min for 4 min (~12 requests).",
             rate_per_min=3, duration_s=240, arrival="constant",
             patience=Patience(impatient_share=0.0)),
]}
