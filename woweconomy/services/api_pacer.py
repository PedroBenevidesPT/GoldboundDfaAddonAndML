r"""One request pacer for every Blizzard-API collector.

There used to be three different answers to "how fast do we go", and the
wrongest one belonged to the collection harvester: a fixed 100 req/s cadence
that never reacted to a rejection, so 32 workers each retried a 429 on a
private timer while the pacer kept handing out slots. That produced 442,492
rejections in seven hours and 2.23 characters/second.

The guild roster crawler's pacer is the one that had been proven against
scale -- it walked 56,126 guild rosters and 3.8M memberships through the same
36k/hour quota. Its three ideas are what make it work, and all three are here:

* **Throttles are coalesced.** Every in-flight request gets its own 429 for the
  same overshoot, so halving per rejection multiplies the reduction and a
  burst of 128 drives the rate to the floor in a millisecond. Rejections
  inside ``THROTTLE_WINDOW`` are one signal, not N -- one rate step and one
  cooldown step, never one per response.
* **One boundary rejection is not an overload.** A run sitting exactly on the
  documented per-second limit clips it occasionally and gets a 429 for a
  fraction of a second. Halving on that would surrender half the throughput
  for the rest of the run over a non-event, so the rate only steps down once
  overshoots have arrived in ``RATE_STEP_AFTER`` separate seconds. Before
  that the cooldown alone waits the second out, and the rate is untouched.
* **A wall of rejections halves twice.** More than ``BURST_HALVINGS`` in a
  window means the ceiling is further away than one halving admits.
* **The rate climbs back.** ``RECOVER_STREAK`` clean successes multiply it up
  again. Without this a single bad minute pins the run at the floor for the
  rest of its life, which is how a 538-hour ETA happens.

Two things were added on top, because a long-running collector needs them:

* **A global cooldown.** The rate says how fast to go; the cooldown says when
  not to go at all. Every worker parks on the same deadline, so N simultaneous
  rejections cost one wait between them rather than N private sleeps.
* **``Retry-After`` is honoured**, when the server sends it, because the
  server knows its bucket better than we do.

On top of all that, a pacer may carry a **sustained-rate token budget** that
mirrors Blizzard's own two-limit model: a hard *per-second* rate and a soft
*hourly* recharge. ``rate`` is the per-second ceiling (100), and
``sustained_rate`` is what the bucket refills at. Leaving ``sustained_rate`` at
the documented quota (36,000/hour = 10/s) keeps the bucket topped up; setting
it to the per-second limit (100) instead lets a run use the full documented
rate, accepting that the hourly quota is then crossed and Blizzard answers with
*slower service* -- not a 429. Either way the per-second ceiling still holds,
and a rejection is only ever the hard limit talking.
The bucket is opt-in (``sustained_rate=None`` keeps the original pure-RPS
behaviour) so collectors that already pace themselves are not re-tuned.

The rate is a ceiling. It starts at the quota and never exceeds it, whatever
direction it moves in.

Copiado de ``C:\collectorproject\wow_api_pacer.py``; nada mudou. É ele que faz
um sweep de colecções durar horas em vez de queimar a quota em minutos.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from logging import getLogger

logger = getLogger("wow_api_pacer")

#: Blizzard's documented model has two independent limits, and they fail
#: differently:
#:
#: * **36,000 requests/hour** = 10/second of continuous recharge. Exceeding it
#:   yields *slower service until traffic decreases* -- degradation, not a 429.
#:   So the hourly quota must be enforced *proactively* (the token bucket),
#:   never discovered from rejections.
#: * **100 requests/second**. Exceeding it returns 429 for the remainder of the
#:   second, until the bucket refreshes -- a sub-second condition.
#:
#: ``SUSTAINED_RATE`` is the first limit; ``MAX_RATE_CAP`` is the second.
SUSTAINED_RATE = 10.0
MIN_RATE = 1.0

#: Rejections arriving within this window of each other are one overshoot.
THROTTLE_WINDOW = 1.0

#: Distinct overshoots tolerated, at the current rate, before the rate moves.
#: Blizzard's per-second limit fails for "the remainder of the second", so a
#: run parked right on the limit clips the boundary now and then and gets a
#: single 429. That is an artefact of the boundary, not an overload: halving
#: on it costs half the throughput for the rest of the run over a non-event.
#: The rate therefore only steps down once rejections have persisted across
#: RATE_STEP_AFTER distinct seconds; up to that point the cooldown (one
#: second) is the whole response and the rate is left alone.
RATE_STEP_AFTER = 3

#: A gap this long with no rejection ends the episode. The next 429 starts the
#: count again rather than continuing it, so stray boundary clips an hour
#: apart can never accumulate into a rate step.
THROTTLE_QUIET_SECONDS = 3.0

#: A burst longer than this drops the rate twice.
BURST_HALVINGS = 8

#: Clean successes before the rate climbs again.
RECOVER_STREAK = 50

#: Multiplier applied on each successful recovery step.
RECOVER_FACTOR = 1.5

#: Fallback cooldown when the server sends no Retry-After, and its ceiling.
#: One second, because without a server hint a 429 is the *per-second* limit:
#: the documented penalty is "the remainder of the second". A larger default
#: (the old 5s, escalating to minutes) punished a sub-second condition as if
#: the hourly quota were gone. ``Retry-After`` still overrides this whenever
#: the server sends it -- e.g. when a reset is genuinely minutes away.
COOLDOWN_SECONDS = 1.0
MAX_COOLDOWN_SECONDS = 120.0


def retry_after_seconds(headers: dict[str, str] | None) -> float | None:
    """Seconds to wait, from whatever the server actually told us.

    ``Retry-After`` is either a delta in seconds or an HTTP date. Blizzard also
    sends ``x-plan-throttle``/``x-plan-reset`` in milliseconds, which is the
    better signal when present: it describes the actual bucket refill rather
    than a rounded hint. Returns None when the server said nothing usable, and
    the caller then falls back to its own bounded backoff.
    """
    if not headers:
        return None
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    for key in ("retry-after", "x-plan-reset", "x-ratelimit-reset"):
        raw = lowered.get(key)
        if not raw:
            continue
        try:
            return max(0.0, float(raw) / (1000.0 if key != "retry-after" else 1.0))
        except (TypeError, ValueError):
            pass
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            continue
        if when is not None:
            return max(0.0, when.timestamp() - time.time())
    return None


@dataclass
class ApiPacer:
    """Global request budget: one slot queue, one rate, one cooldown.

    Shared by every worker. Concurrency is deliberately *not* the control here
    -- a high worker count just means the scheduler stays saturated while the
    pacer decides the actual rate. The lock in :meth:`slot` is what stops
    every worker reading the same ``_next_slot``, finding no wait, and
    releasing together as a thundering herd.
    """

    #: The ceiling. The live rate moves below it and climbs back to it, but
    #: never above: a rate learned from rejections is a worse ceiling than
    #: the documented quota. Defaults to the absolute cap because the roster
    #: crawler has always started here and adapted down; long collectors that
    #: must not outrun the bucket should pass ``SUSTAINED_RATE`` explicitly.
    rate: float = 100.0
    min_rate: float = MIN_RATE
    #: Set False to pin the rate at ``min_rate`` after a rejection instead of
    #: recovering, which is only ever useful for a bounded burst.
    recover: bool = True
    recover_streak: int = RECOVER_STREAK
    cooldown_seconds: float = COOLDOWN_SECONDS
    max_cooldown_seconds: float = MAX_COOLDOWN_SECONDS
    #: Sustained rate in requests/second enforced by the token bucket, or
    #: ``None`` to disable the bucket and keep the original pure-RPS model.
    #: Set it to ``SUSTAINED_RATE`` for any collector long enough to drain the
    #: hourly quota.
    sustained_rate: float | None = None
    #: Tokens the bucket can hold before it stops refilling, i.e. the width of
    #: the allowed burst. Defaults to the burst ceiling (one second at
    #: ``rate``), so a fresh run may start fast and then settle onto the
    #: sustained rate.
    burst_capacity: float | None = None

    _rate: float = 0.0
    _interval: float = 0.0
    _next_slot: float = 0.0
    _cooldown_until: float = 0.0
    _streak: int = 0
    #: Distinct overshoots in the current episode of trouble. Drives both the
    #: rate step (only from ``RATE_STEP_AFTER`` on) and the no-hint backoff
    #: exponent. Reset by a success, but only after ``THROTTLE_QUIET_SECONDS``
    #: of quiet -- see :meth:`on_success`.
    _consecutive: int = 0
    _last_throttle: float = 0.0
    _throttle_burst: int = 0
    #: Token bucket state. ``_bucket_ready`` distinguishes "bucket not started"
    #: from "bucket empty": the first slot fills it, so a run does not begin
    #: with a phantom burst it never earned.
    _tokens: float = 0.0
    _bucket_updated: float = 0.0
    _bucket_ready: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # Telemetry. A run this long has to be able to show requests/s and 429s/s,
    # not a single "throttled" line.
    requests: int = 0
    successes: int = 0
    throttled: int = 0
    latency_total: float = 0.0
    latency_samples: int = 0
    cooldown_events: int = 0
    cooldown_seconds_total: float = 0.0
    last_retry_after: float | None = None

    def __post_init__(self) -> None:
        self._rate = max(self.min_rate, min(self.rate, MAX_RATE_CAP))
        self._interval = 1.0 / self._rate if self._rate > 0 else 0.0
        if self.sustained_rate is not None and self.sustained_rate <= 0:
            # A zero or negative sustained rate would make the bucket a deadlock;
            # treat it as "no bucket" rather than a run that never sends.
            self.sustained_rate = None
        # _last_throttle stays at its 0.0 default on purpose: the first
        # rejection of a run is a real signal, not a continuation of a burst
        # that started before the pacer existed.
        self._next_slot = 0.0

    # -- introspection ---------------------------------------------------

    @property
    def current(self) -> float:
        """The rate in force right now."""
        return self._rate

    @property
    def cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until - time.monotonic())

    @property
    def avg_latency(self) -> float:
        if not self.latency_samples:
            return 0.0
        return self.latency_total / self.latency_samples

    # -- the two things callers use --------------------------------------

    def _bucket_capacity(self) -> float:
        """How many tokens the bucket holds between refills."""
        if self.burst_capacity is not None:
            return max(1.0, float(self.burst_capacity))
        return max(1.0, self._rate)

    def _refill_tokens(self, at: float) -> float:
        """Advance the bucket to ``at`` and return the tokens available there.

        The first call fills the bucket to capacity; later ones add the elapsed
        time at the sustained rate, capped at capacity. Called with a future
        ``at`` this *projects* the bucket forward without granting anything,
        which is how the wait for the next token is computed.
        """
        capacity = self._bucket_capacity()
        if not self._bucket_ready:
            self._tokens = capacity
            self._bucket_ready = True
            self._bucket_updated = at
            return self._tokens
        elapsed = max(0.0, at - self._bucket_updated)
        self._tokens = min(capacity, self._tokens + elapsed * float(self.sustained_rate))
        self._bucket_updated = at
        return self._tokens

    async def slot(self) -> None:
        """Take a request slot: cooldown, cadence, then the sustained budget.

        The lock is held across the wait, so the three gates -- cooldown,
        burst cadence and token budget -- are all global: concurrency decides
        how many workers are parked, never how fast they leave. That is what
        stops N workers from each spending the hour's quota at once.
        """
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            begin = max(now, self._next_slot, self._cooldown_until)
            sustained = self.sustained_rate
            if sustained is not None:
                tokens = self._refill_tokens(begin)
                if tokens < 1.0:
                    # Project to the moment the bucket earns the next token,
                    # then refill to exactly that point so the debit below
                    # leaves a truthful balance instead of a negative one.
                    begin = max(
                        begin, self._bucket_updated + (1.0 - tokens) / float(sustained)
                    )
                    self._refill_tokens(begin)
            if begin > now:
                await asyncio.sleep(begin - now)
            # Booked from when the request starts, never from a backlog: after
            # a cooldown the next slot is one interval away, so resuming does
            # not release every parked worker at the same instant. That herd
            # is what earned the next burst of rejections.
            self._next_slot = begin + self._interval
            if sustained is not None:
                self._refill_tokens(begin)
                self._tokens = max(0.0, self._tokens - 1.0)
            self.requests += 1

    def on_throttled(
        self, headers: dict[str, str] | None = None, *, latency: float | None = None
    ) -> float:
        """React to rejections. Returns the cooldown imposed, in seconds.

        Two independent things happen, because they answer different
        questions. The *cooldown* opens, and every worker waits it out together
        instead of each sleeping privately. The *rate* steps down only once the
        rejections have persisted across ``RATE_STEP_AFTER`` distinct seconds:
        one boundary clip costs a wait, not half the throughput.
        """
        self.throttled += 1
        if latency is not None:
            self.latency_total += latency
            self.latency_samples += 1
        now = time.monotonic()
        if now - self._last_throttle < THROTTLE_WINDOW:
            # Same overshoot: count it, but do not react again. Reacting per
            # *response* is how 32 simultaneous 429s became 32 cooldown steps
            # and a run parked for minutes over a sub-second condition.
            self._throttle_burst += 1
        else:
            burst = self._throttle_burst
            gap = now - self._last_throttle
            self._last_throttle = now
            self._throttle_burst = 0
            # Escalation is per distinct overshoot, not per rejection, so a
            # burst costs one step. A long quiet gap ends the episode: isolated
            # clips are separate events and must not add up to a rate step.
            self._consecutive = (
                1 if gap > THROTTLE_QUIET_SECONDS else self._consecutive + 1
            )
            if self._consecutive >= RATE_STEP_AFTER:
                # Now the rejections are a pattern rather than a boundary clip,
                # and giving up rate is worth it to make them stop.
                self._rate = max(self.min_rate, self._rate * 0.5)
                if burst > BURST_HALVINGS:
                    # A wall of rejections means the ceiling is further away
                    # than one halving admits.
                    self._rate = max(self.min_rate, self._rate * 0.5)
                # The cadence has to move with the rate, or the step is
                # cosmetic: the pacer would report 50 req/s and go on handing
                # out slots 100-to-the-second.
                self._interval = 1.0 / self._rate
            # Re-anchor rather than clear the schedule: clearing it let every
            # queued worker through at once, and that herd earned the next
            # burst of 429s.
            self._next_slot = max(self._next_slot, now + self._interval)
        self._streak = 0

        retry_after = retry_after_seconds(headers)
        self.last_retry_after = retry_after
        if retry_after is None:
            # The exponent is bounded before the shift: a sustained overload
            # can produce thousands of consecutive rejections, and 2**5000 is
            # not a number. Clamping here is what makes the cap below the only
            # thing standing between a storm and an OverflowError.
            #
            # Flat until the rate itself moves (see RATE_STEP_AFTER): the whole
            # answer to a boundary clip is to wait out the remainder of the
            # second, not to back off as if the limit were minutes away.
            escalation = max(0, self._consecutive - (RATE_STEP_AFTER - 1))
            backoff = self.cooldown_seconds * (2 ** min(escalation, 16))
            wait = min(backoff, self.max_cooldown_seconds)
        else:
            wait = min(retry_after, self.max_cooldown_seconds)
        # max(), not += : simultaneous rejections extend the shared deadline
        # by one Retry-After between them, not by one each.
        self._cooldown_until = max(self._cooldown_until, time.monotonic() + wait)
        self.cooldown_events += 1
        self.cooldown_seconds_total += wait
        return wait

    def on_success(self, latency: float = 0.0) -> None:
        """Record a good request and, after a clean streak, climb back up.

        The clean streak is counted here rather than taken from the caller.
        The roster crawler used to pass its own running total positionally,
        while the collection harvester passed a latency -- one parameter, two
        meanings, and the wrong reading was silent. Counting in one place
        removes the ambiguity.
        """
        self.successes += 1
        self.latency_total += latency
        self.latency_samples += 1
        # The episode ends on *quiet*, not on one success. At 100 req/s a
        # boundary clip is followed by dozens of successes inside the same
        # second, so clearing the count per success would erase every sustained
        # signal before it could ever add up. Only once the rejections have
        # actually stopped does a success reset the count.
        if time.monotonic() - self._last_throttle >= THROTTLE_QUIET_SECONDS:
            self._consecutive = 0
        self._streak += 1
        if (
            self.recover
            and self._streak >= self.recover_streak
            and self._rate < self.rate
        ):
            self._rate = min(self.rate, self._rate * RECOVER_FACTOR)
            self._interval = 1.0 / self._rate
            self._streak = 0
            # The schedule is not cleared: queued slots keep their spacing and
            # the higher rate applies to slots handed out from now on.
            logger.info("rate up to %.1f req/s", self._rate)


#: Absolute clamp. A documented ceiling, not a target: the bucket refills at
#: ``SUSTAINED_RATE`` and nothing learned from rejections can raise the total
#: a day can carry.
MAX_RATE_CAP = 100.0


def retry_after_seconds_aliased(headers: dict[str, str] | None) -> float | None:
    """Backwards-compatible alias; see :func:`retry_after_seconds`."""
    return retry_after_seconds(headers)
