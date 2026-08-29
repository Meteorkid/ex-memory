.PHONY: install test lint typecheck check run dev push-all docker-build clean unlock eval eval-corpus eval-retrieval eval-generation eval-report

# 可用 make <target> PYTHON=.venv/bin/python 等指定解释器，默认沿用 PATH
PYTHON ?= python
PIP ?= pip
PYTEST ?= pytest

install:
	$(PIP) install -r requirements.txt -r requirements-dev.txt

test:
	$(PYTEST) --cov --cov-report=term

test-verbose:
	$(PYTEST) --cov --cov-report=term -v

lint:
	ruff check .

typecheck:
	mypy

# 提交前的完整检查
check: lint typecheck test

lint-fix:
	ruff check --fix .

run:
	$(PYTHON) -m server.app

dev:
	uvicorn server.app:app --reload --host 0.0.0.0 --port 8000

# --- RAG 评测 ---
eval-corpus:
	$(PYTHON) -m evals.build_corpus

eval-retrieval:
	$(PYTHON) -m evals.run_eval retrieval

eval-generation:
	$(PYTHON) -m evals.run_eval generation

eval-report:
	$(PYTHON) -m evals.run_eval report

eval:
	$(PYTHON) -m evals.run_eval all

# --- 推送 ---
# 三个远端必须保持同步；先校验每个远端都不领先本地，避免 push 被拒或误覆盖他人提交
REMOTES ?= origin gitee gitlab
BRANCH  ?= main

push-all:
	@git fetch --all --quiet
	@fail=0; for r in $(REMOTES); do \
	  if ! git rev-parse --verify --quiet $$r/$(BRANCH) >/dev/null; then \
	    echo "  ? $$r/$(BRANCH) 不存在，将新建"; continue; \
	  fi; \
	  ahead=$$(git rev-list --count $(BRANCH)..$$r/$(BRANCH)); \
	  if [ "$$ahead" != "0" ]; then \
	    echo "  x $$r/$(BRANCH) 有 $$ahead 个本地没有的 commit"; fail=1; \
	  else \
	    echo "  v $$r/$(BRANCH) 可快进"; \
	  fi; \
	done; \
	if [ $$fail = 1 ]; then \
	  echo ""; \
	  echo "中止：请先 git rebase 到对应远端，不要 force push"; exit 1; \
	fi
	@echo ""
	@for r in $(REMOTES); do echo "-> $$r"; git push $$r $(BRANCH) || exit 1; done
	@echo ""
	@echo "三端已同步到 $$(git rev-parse --short $(BRANCH))"

docker-build:
	docker compose build

docker-up:
	docker compose up -d

docker-down:
	docker compose down

# 清理崩溃的 git 进程遗留的陈旧锁文件（确认无 git 进程运行后再用）
unlock:
	rm -f .git/index.lock

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true
	find . -type f -name "*.pyc" -delete 2>/dev/null; true
	rm -rf htmlcov .coverage .pytest_cache 2>/dev/null; true
