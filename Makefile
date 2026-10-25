COMPOSE := docker compose -f lab/docker-compose.yml
S ?= redis-down

.PHONY: help install check lint format typecheck test lab-up lab-down lab-reset lab-ps lab-logs doctor scenario scenario-plan eval

help:
	@echo "install        install Python dependencies (uv)"
	@echo "check          lint + typecheck + test"
	@echo "lab-up         build and start the lab"
	@echo "lab-down       stop the lab, keep data"
	@echo "lab-reset      stop the lab and delete its data"
	@echo "doctor         check that Prometheus, Loki and the shop target are up"
	@echo "scenario S=id  run a fault scenario (default: redis-down)"
	@echo "scenario-plan  print what a scenario will do without running it"
	@echo "eval S=id N=10 run a scenario N times and score the investigations"

install:
	uv sync

check: lint typecheck test

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy

test:
	uv run pytest

lab-up:
	$(COMPOSE) up -d --build

lab-down:
	$(COMPOSE) down

lab-reset:
	$(COMPOSE) down -v

lab-ps:
	$(COMPOSE) ps

lab-logs:
	$(COMPOSE) logs -f --tail=100

doctor:
	uv run assay doctor

scenario:
	uv run python lab/scenario_runner.py $(S)

scenario-plan:
	uv run python lab/scenario_runner.py $(S) --dry-run

N ?= 10

eval:
	uv run python eval/run_eval.py $(S) --runs $(N)
