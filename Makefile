# Makefile for Cutter API
.PHONY: help build start dev down tests tests-local benchmark create-test-db lint-fix lint-check install clean logs redis-cli redis-flush

# Tests ALWAYS run against a dedicated database (cutter_test_db), never
# against the development database (cutter_db): the conftest TRUNCATEs per test.
DB_TEST_DOCKER = postgresql://cutter:cutter@postgres:5432/cutter_test_db
DB_TEST_LOCAL = postgresql://cutter:cutter@localhost:5433/cutter_test_db

help: ## Shows this help
	@grep -E '^[A-Za-z0-9_.-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'

install: ## Installs dependencies
	pip install -r requirements_dev.txt

build: ## Builds the Docker image
	docker compose build

start: ## Starts the containers in daemon mode
	docker compose up -d

dev: ## Starts development mode with hot-reload
	docker compose up -d && docker rm -f api || true && docker compose run --rm -e ENVIRONMENT=local -p 8000:8000 api

down: ## Stops the containers
	docker compose down

logs: ## Shows the application logs
	docker compose logs -f api

create-test-db: ## Creates cutter_test_db if missing (idempotent, never touches cutter_db)
	docker compose up -d postgres
	@until docker compose exec -T postgres pg_isready -U cutter >/dev/null 2>&1; do sleep 1; done
	@docker compose exec -T postgres psql -U cutter -d cutter_db -tc "SELECT 1 FROM pg_database WHERE datname='cutter_test_db'" | grep -q 1 || \
		docker compose exec -T postgres psql -U cutter -d cutter_db -c "CREATE DATABASE cutter_test_db"

# `-m "not benchmark"` mirrors CI exactly: the parity benchmarks depend on the
# OR-Tools build (see the `benchmark` target below), so they belong to
# `make benchmark`, not to the suite everyone runs on their own machine.
tests: create-test-db ## Runs the tests (against cutter_test_db; never touches cutter_db)
	docker compose run --no-deps --rm -e DATABASE_URL=$(DB_TEST_DOCKER) -e TEST_DATABASE_URL=$(DB_TEST_DOCKER) -e BCRYPT_ROUNDS=4 api pytest -q -m "not benchmark"

tests-local: create-test-db ## Runs the tests locally (PostgreSQL on localhost:5433)
	DATABASE_URL=$(DB_TEST_LOCAL) TEST_DATABASE_URL=$(DB_TEST_LOCAL) BCRYPT_ROUNDS=4 pytest -q -m "not benchmark"

# The `benchmark` tests pin the exact board count of the audit cut lists. CP-SAT
# picks arbitrarily among equally optimal packings, and that pick is reproducible
# only for a given OR-Tools BUILD, so a macOS arm64 wheel and the manylinux
# x86_64 one legitimately disagree. Only one build's numbers are commercially
# meaningful: the one that ships (build-push.yml publishes linux/amd64), so the
# benchmark always runs there — emulated on Apple Silicon, hence the slowness.
benchmark: ## Runs the parity benchmarks against the production build (linux/amd64)
	docker build --platform linux/amd64 --target dev -t cutter-benchmark .
	docker run --rm --platform linux/amd64 -e DATABASE_URL=$(DB_TEST_DOCKER) \
		-v "$(CURDIR)":/src cutter-benchmark pytest -m benchmark --no-cov -q -p no:cacheprovider

lint-fix: ## Fixes formatting and lint errors
	source .venv/bin/activate && ruff check --fix . && ruff format .

lint-check: ## Checks code formatting
	docker compose run --no-deps --rm api ruff check .
	docker compose run --no-deps --rm api ruff format --check .

lint-check-local: ## Checks code formatting locally
	ruff check . && ruff format --check .

clean: ## Cleans up unused containers and images
	docker system prune -f
	docker volume prune -f

shell: ## Opens a shell inside the container
	docker compose run --rm api bash

redis-cli: ## Opens redis-cli inside the Redis container
	docker exec -it redis redis-cli

redis-flush: ## Clears the whole Redis cache (FLUSHALL)
	docker exec -it redis redis-cli FLUSHALL

run-local: ## Runs the application locally (requires PostgreSQL on localhost:5433)
	ENVIRONMENT=local DATABASE_URL=postgresql://cutter:cutter@localhost:5433/cutter_db python main.py

seed-boards: ## Seeds/updates boards and edge bandings into local PostgreSQL (5433). Use reset=1 for a hard rebuild.
	DATABASE_URL=postgresql://cutter:cutter@localhost:5433/cutter_db .venv/bin/python scripts/seed_boards.py $(if $(reset),--reset)

seed-admin: ## Creates the first administrator from ADMIN_EMAIL/ADMIN_PASSWORD in .env
	.venv/bin/python scripts/seed_admin.py

seed-demo: ## Seeds demo data (branches/users/clients/pre-orders/orders by status) into local PostgreSQL (5433). Use reset=1 to regenerate.
	DATABASE_URL=postgresql://cutter:cutter@localhost:5433/cutter_db REDIS_URL=redis://localhost:6379/0 .venv/bin/python scripts/seed_demo.py $(if $(reset),--reset)

setup: ## Initial project setup
	cp .env.example .env || true
	@echo "Archivo .env creado. Edítalo según tus necesidades."

migrations:  ## Create a new migration with Alembic
	docker compose run --no-deps --rm api alembic revision --autogenerate -m "$(m)"

upgrade:  ## Apply all pending migrations with Alembic
	docker compose run --no-deps --rm api alembic upgrade head

downgrade:  ## Downgrade the database to the previous migration
	docker compose run --no-deps --rm api alembic downgrade $(d)