# Capacity and operating limits

BananaChat separates HTTP/chat storage capacity from inference capacity. The web/compute split lets the web service remain available while a compute machine is busy or offline. Real model latency and concurrency depend on GPU memory, model size, context, Ollama, and any image-generation backend; benchmark that hardware separately.

## Measured HTTP and scheduling workload

On 9 September 2026, the web application ran on Debian 13 with Gunicorn 26.2.0, two workers, 16 threads per worker, and affinity to two AMD EPYC 9354P cores. The shared host had 32 GiB of RAM and also ran tests, a disposable VM and unrelated work. Its synthetic database held 65 users and a 25,000-message chat, with 500 characters per message. History requests loaded a bounded page of messages.

| Workload | Requests | Throughput | 95th percentile latency | Result |
| --- | ---: | ---: | ---: | --- |
| History reads, 1 concurrent client | 12 | 21.56/s | 73 ms | All HTTP 200 |
| History reads, 8 concurrent clients | 96 | 66.68/s | 212 ms | All HTTP 200 |
| History reads, 16 concurrent clients | 96 | 74.89/s | 345 ms | All HTTP 200 |
| Inference protocol fixture, 8 clients | 64 | 9.10/s | 1,116 ms | All HTTP 200 |
| Slow inference fixture, 64 simultaneous clients | 64 | - | - | 24 HTTP 200, 40 controlled HTTP 503 |
| Five-minute mixed workload: history reads | 12,672 | 42.16/s | 428 ms | All HTTP 200 |
| Five-minute mixed workload: fixture generations | 1,056 | 3.51/s | 982 ms | All HTTP 200 |

**Inference used a deterministic local HTTP fixture. No GPU or real model throughput was measured.** The overload burst checked bounded admission, the explicit busy response and its retry header. Its successful requests completed in under eight seconds; those times describe only the fixture. The continuous workload used 16 clients and repeated batches of 96 reads plus eight generations.

During overload, all 46 health probes succeeded with 89 ms 95th percentile latency. During continuous traffic, all 761 health probes succeeded with 322 ms 95th percentile latency. The observed queue peak was 24; it emptied after completion. Usage records matched all 1,144 successful inference requests across the run, and SQLite's consistency check passed. Peak combined proportional application memory was approximately 182 MiB, excluding any real inference backend; summed resident memory was approximately 244 MiB. Proportional memory increased from 108 to 133 MiB during the five-minute phase, which included worker recycling.

These measurements establish behavior for this workload on a shared host. They do not establish multi-day memory stability or a production service-level guarantee.

Gunicorn 26.2 or newer is required: the previous threaded server could drop accepted connections while recycling a worker. The regression test sends 400 concurrent requests through repeated replacements without retries. The application also lets recycling workers drain for the configured generation/image deadline. Requested service shutdowns remain bounded by systemd's stop deadline, so clients should reconnect after planned maintenance.

## Admission, storage, and failure behavior

Inference admission is bounded before an accepted stream starts. Each worker reserves HTTP threads for control traffic. Database leases coordinate tasks across web workers, queued requests have deadlines and per-owner limits, and disconnect/stop paths release capacity. The application bounds history pages, context, output, buffered stream data and remote worker chunks. Overload returns HTTP 503; account rate limits can return HTTP 429. API rate limits follow the authenticated owner across their tokens, including when multiple owners share an IP address.

Saved chat outcomes and usage are committed before terminal success is announced. Interrupted replies retain checkpoints. Fallback selects an authorized available model before output begins and does not splice multiple models' partial responses together. Restoring a backup preserves chats and leaves model downloads pending an administrator's choice.

Keep SQLite on reliable local storage and one writable web deployment per database. Increase compute capacity independently, leaving web memory and HTTP threads available for login, status, cancellation and administration. Measure real tokens per second, first-token latency, GPU memory, queue wait, request failures and disk growth with your models before setting production limits. See [deployment](deployment.md) and [configuration](configuration.md).
