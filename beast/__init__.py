"""Beast - self-running surveillance of how meta-analysis evidence evolves over time.

Beast periodically ingests the trial set for a tracked topic, recomputes a
random-effects pooled estimate (effect, CI, prediction interval, I-squared),
stores a timestamped snapshot, and flags meaningful changes versus the previous
snapshot (effect shift, significance flip, new trials, heterogeneity change).

See the README for the architecture and how to run it self-running.
"""

__version__ = "0.1.0"
