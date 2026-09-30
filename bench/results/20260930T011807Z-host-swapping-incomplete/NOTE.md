# Invalid: the Mac was swapping; run incomplete

The second run (with the thread limit fixed) was taken while the host was
under heavy memory pressure: 12.7 GB of 14.3 GB swap in use and, measured
right after, 813 MB swapped out in 20 s with the SurgiServe stack stopped.
Docker's 4 GB VM is backed by host memory, so its pages were being swapped
too, even though the VM itself reported free memory.

Symptoms: the first configuration ran 23% slower than in the previous run,
the 4-worker runs collapsed (0.47 and 1.38 jobs/s, with request failures),
and `docker compose up` for the last configuration failed. No results.json
was written; `run.log` has the per-configuration lines that were.

`bench/run.py` now records host swap-outs during each run, flags runs where
the host swapped, and refuses to start if the host is already swapping.
