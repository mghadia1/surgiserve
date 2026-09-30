# Explain it yourself

Questions to answer out loud, without notes, before this project goes on a
resume or into an interview. There are no answers here on purpose: the point
is that the explanation is yours. If one stumps you, read the code it points
to, run the thing, and try again.

## Run it first

1. Bring the stack up (`make weights`, then `make up`), upload a frame, and
   download its mask. Then run `docker compose logs --no-log-prefix api worker
   | grep <job id>` and say what each line tells you.
2. Scale to two workers, kill one with `docker kill -s KILL` while it holds a
   job (a slow fake model helps: `SURGISERVE_MODEL=fake
   SURGISERVE_FAKE_MODEL_DELAY_SECONDS=8`), and watch the job finish anyway.
   How long did recovery take, and which setting decides that?

## Design choices (pick one to defend)

3. Why does Redis carry only job ids, while Postgres holds everything else?
   What would go wrong if the queue carried the whole job?
4. Walk through `POST /jobs`: file, row, queue. For each point where the
   process could die, what state is left behind, and what cleans it up?
   (`src/surgiserve/jobs.py`)
5. What does `BLMOVE` do that `BRPOP` would not, and what would you lose if
   the worker used `BRPOP`? (`src/surgiserve/job_queue.py`)
6. Every status change is a conditional `UPDATE`. Pick one and say what race
   the condition prevents. Why does `_finish` also match on the attempt
   number? (`src/surgiserve/worker.py`)
7. Why are `/health` and `/health/ready` separate? What would Docker do if
   the container's health check used readiness during a database outage?
8. Why is the status column a VARCHAR with a CHECK constraint, and when did
   that pay off? (`migrations/versions/0003_*.py`)

## Failure modes (pick one to tell as a story)

9. The worker-id bug: why did `hostname-pid` collide, what did the symptom
   look like, and how was it found?
10. The thread limit: why did a worker "limited to 1 thread" use 250% CPU, and
    why does the fix have to happen before `import torch`?
11. Why is the benchmark run from the day the Mac was swapping unusable, even
    though Docker reported free memory inside its VM?
12. The model traps: what happens if the network is built without
    `normalize_for_imagenet=True`, and why would nothing crash? Which test
    catches it?

## Numbers you should be able to source

13. Where does "bit-identical to the frozen benchmark on all 118 test images"
    come from, and what exactly was compared? What is *not* covered by that
    claim? (Hint: mask size.)
14. What does the Dice 0.9226 measure, and why is it not a claim about this
    service's full-size masks, or about patients?
