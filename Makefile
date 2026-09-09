.PHONY: install lint test demo build
install:
	uv sync --frozen --extra demo --group dev
lint:
	uv run --frozen ruff check .
	uv run --frozen ruff format --check .
test:
	uv run --frozen pytest --cov=api_sentinel --cov-report=term-missing

demo:
	bash scripts/docker_demo.sh
build:
	uv build
