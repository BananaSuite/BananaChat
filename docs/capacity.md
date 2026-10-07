# Capacity and operating limits

BananaChat separates web capacity (pages, chat history, accounts) from
inference capacity (the models). With the web/compute split the site stays
available while the compute machine is busy or offline. Real model latency
and concurrency depend on GPU memory, model size, context length, Ollama and
any image backend; benchmark that hardware separately.

## Measured web and admission behaviour

Measured on 3 October 2026 with `python scripts/load_test.py`, which
starts the real Gunicorn service (`gunicorn.conf.py`: two processes, 16
threads each) against a temporary instance and an imitation Ollama server
that streams 60 tokens 10 ms apart. The host was a shared 8-vCPU container
with 32 GiB of RAM (Python 3.14.4, Gunicorn 26.2.0) that was also
running the test suite, so the numbers are conservative and vary between
runs. The database held 65 accounts and one chat with 25,000 messages of 500
characters; the chat page loads a bounded page of the newest messages.

| Workload | Requests | Throughput | 95th percentile | Result |
| --- | ---: | ---: | ---: | --- |
| Chat page with 25,000 messages, 1 client | 24 | 35.9/s | 30 ms | All HTTP 200 |
| Chat page with 25,000 messages, 8 clients | 96 | 150.5/s | 93 ms | All HTTP 200 |
| Chat page with 25,000 messages, 16 clients | 96 | 163.1/s | 147 ms | All HTTP 200 |
| API completions, 8 accounts in parallel | 48 | 5.1/s | 1,753 ms | All HTTP 200 |
| Burst: 64 simultaneous completions, 64 accounts | 64 | – | 4,646 ms | 24 HTTP 200, 40 HTTP 503 `server_busy` |
| Burst: 16 simultaneous completions, one account | 16 | – | 1,279 ms | 3 HTTP 200, 6 HTTP 429 `rate_limit_exceeded`, 7 HTTP 429 `too_many_requests` |
| `/health` probes during both bursts (5 s timeout) | 130 | – | 4 ms | All HTTP 200 |

**Inference used an imitation server; no GPU or real model throughput was
measured.** With the default `BC_MAX_CONCURRENT=4` and 0.6 s per answer the
imitation allows at most about 6.7 answers per second, so 5.1/s shows the
queue overhead, not model speed.

What the run shows:

- **Monitors are never starved.** Each Gunicorn process keeps four threads
  that generating requests may not take (`security.RESERVED_THREADS`), so
  health checks, `/status`, sign-in, stop buttons and administration stay
  fast while every other thread streams an answer. Requests over that cap
  get an immediate HTTP 503 with `Retry-After: 5` instead of waiting.
- **Per-account fairness.** An account may have three requests admitted at
  once (one running); further ones get HTTP 429.
- **Exact accounting.** The ledger held one entry per successful request (75)
  and none for refused ones; SQLite's integrity check passed.
- **Memory.** The two Gunicorn processes used about 286 MiB resident after
  the run, excluding any inference backend.

This is one short run on a shared host. It does not establish multi-day
memory stability or a service-level guarantee; run the script on your own
hardware (it prints the table above) before setting production limits.

Gunicorn 26.2 or newer is required: older threaded workers could drop
accepted connections while recycling a process. `tests/test_gunicorn_recycling.py`
sends 400 concurrent requests through repeated worker replacements without
retries.

## CPU inference in containers

Match Ollama's computation threads to the CPUs actually available to its
container. In a disposable two-CPU check with Ollama 0.35.1, its automatic
eight-thread choice caused long delays and a controlled BananaChat timeout.
The same official Qwen3 0.6B weights with two computation threads completed
the API, saved-chat, Stop and interrupted-accounting checks. This behavior is
consistent with Ollama's [reported CPU-quota issue](https://github.com/ollama/ollama/issues/17916).

For a two-CPU deployment, create a model with a matching thread default:

```text
FROM qwen3:0.6b
PARAMETER num_thread 2
```

Save this as `Modelfile`, run `ollama create qwen3-cpu:0.6b -f Modelfile`,
then synchronize and review that model in BananaChat. Choose the count for
your own CPU allocation; GPU deployments need their own measurements. The
derived model shares its base weights, and its thread default applies to
ordinary BananaChat requests.

## Admission, storage and failure behaviour

Admission is decided before an accepted stream starts. A shared queue in the
database coordinates all web processes: `BC_MAX_CONCURRENT` requests run at
once, up to `BC_MAX_QUEUE_DEPTH` wait (HTTP 503 `queue_full` beyond that),
and waiting requests give up after `BC_QUEUE_TIMEOUT`. Waiting requests are
started as soon as a slot frees; stop buttons and disconnects release their
place at once. History pages, context length, output length, buffered stream
data and remote worker chunks are all bounded. API rate limits
(`api_rpm`) follow the account across its tokens, even when several
accounts share an address.

Answers and usage are committed before success is reported. An interrupted
answer keeps what was written. Fallback switches to another permitted model
only before any output, so answers never mix models. Restoring a backup keeps
chats and leaves model downloads waiting for an administrator's decision.

Keep SQLite on reliable local storage, with one web deployment writing to
each database. Scale compute independently of the web server. Measure real
tokens per second, time to first token, GPU memory, queue wait, failures and
disk growth with your own models before choosing limits. See
[deployment](deployment.md) and [configuration](configuration.md).
