# Do not use these numbers for the thread comparison

This run set the per-worker thread limit with `torch.set_num_threads` only.
In this image that does not limit the convolutions: on ARM they run through
oneDNN, whose thread pool is sized from `OMP_NUM_THREADS` when it starts.
Measured afterwards, a worker "limited to 1 thread" put 68% of its forward
pass CPU time on other threads and used about 2.5 CPUs under load.

So the 1t, 2t and 4t rows here are mislabelled: those workers ran more
threads than the label says. The 8t rows (PyTorch's default) are what they
say. The worker now also sets `OMP_NUM_THREADS` before importing torch
(`limit_threads` in `src/surgiserve/worker.py`); the run that replaced this
one is the next directory.

Kept, not deleted, because it is how the problem was found: a "1 thread"
worker showing 250% CPU in `docker stats`.
