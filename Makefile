.PHONY: install test lint run dev docker-build clean

install:
	pip install -r requirements.txt -r requirements-dev.txt

test:
	pytest --cov --cov-report=term

test-verbose:
	pytest --cov --cov-report=term -v

lint:
	ruff check .

lint-fix:
	ruff check --fix .

run:
	python -m server.app

dev:
	uvicorn server.app:app --reload --host 0.0.0.0 --port 8000

docker-build:
	docker compose build

docker-up:
	docker compose up -d

docker-down:
	docker compose down

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true
	find . -type f -name "*.pyc" -delete 2>/dev/null; true
	rm -rf htmlcov .coverage .pytest_cache 2>/dev/null; true
