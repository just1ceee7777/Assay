# Assay

Assay helps you find out why a self-hosted service broke. It reads the metrics, logs and
container events you already collect and tells you which explanation the data supports,
which it contradicts, and what is still missing to be sure.

> **Status: early development.** Investigation is not implemented yet. What works today
> is the `assay doctor` command and a lab environment where you can break a demo service
> on purpose and watch it happen in Prometheus, Loki and Grafana.

## What Assay will and will not do

- **Read-only.** Assay never changes the system it observes.
- **Every claim has evidence.** A statement in a report links to the metric, log line or
  event it is based on. A statement without evidence does not appear in the report.
- **"Not enough data" is a valid answer.** When the data cannot tell causes apart,
  Assay says so and names the signal that would help.
- **Works without an LLM.** A language model, if you enable one, only rewords findings
  into plain language. It cannot add claims of its own.

## Requirements

- Linux with [Docker Engine](https://docs.docker.com/engine/install/) and the Docker
  Compose plugin
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- About 4 GB of free RAM for the lab

## Quick start: the lab

The lab runs a small shop API with PostgreSQL and Redis under steady load, plus
Prometheus, Loki and Grafana watching it.

```bash
git clone https://github.com/just1ceee7777/Assay.git
cd Assay
make install
make lab-up
make doctor
```

The first start builds images and can take a few minutes. Run `make doctor` again until
every line shows `ok`.

| What | Where |
| --- | --- |
| Shop API | http://localhost:8000/items/1 |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3000 |

All ports listen on `127.0.0.1` only. Grafana runs without a login, which is fine for a
local lab and not fine anywhere else.

To stop the lab, run `make lab-down`. To stop it and delete its data, run `make lab-reset`.

## Break something on purpose

Scenarios inject a known fault into the lab, then undo it.

```bash
make scenario-plan S=redis-down   # show the steps without running them
make scenario S=redis-down        # run it, about 7 minutes
```

While it runs, watch the error rate in Grafana or Prometheus:

```promql
sum by (status) (rate(shop_http_requests_total[1m]))
```

and the matching log lines in Grafana's Explore view with Loki:

```logql
{service="app"} | json | msg="dependency_error"
```

Each run writes a record with the time of every phase to `lab/runs/`.

| Scenario | What happens |
| --- | --- |
| `redis-down` | Redis is stopped for 3 minutes, item reads fail with HTTP 503 |

## Commands

| Command | What it does |
| --- | --- |
| `assay doctor` | Checks that Prometheus and Loki answer and that the shop is being scraped |
| `assay --version` | Prints the version |

Run them with `uv run assay ...` from the repository folder.

## License

[Apache License 2.0](LICENSE)
