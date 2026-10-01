# Superseded: single pass, 16% drift, swap-ins not recorded

Taken after other apps were quit (0 MB swapped out in the 10 s before it
started). It completed with no failed jobs, but:

- 8 of 9 runs had the host swap out 66-320 MB, over the runner's limit at
  the time (50 MB of swap-outs). Swap-ins, which are what would stall the
  service, were not recorded, so it cannot be said whether that mattered.
- The repeat of the first configuration (1 worker, 8 threads) came out 16%
  slower than the first run (4.92 -> 4.13 jobs/s) although it swapped less,
  which points at the laptop warming up rather than swapping. macOS recorded
  no thermal warning.

So the runner now runs every configuration twice (forward, then reverse
order, with cool-downs) and judges validity on swap-ins. The next directory
is that run.
