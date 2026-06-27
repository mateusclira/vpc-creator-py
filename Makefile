.PHONY: install run test lint format clean

install:
	pip install -r requirements.txt

run:
	uvicorn main:app --reload --host 0.0.0.0 --port 8000

test:
	pytest tests/ -v

lint:
	ruff check main.py database.py tests/

format:
	black main.py database.py tests/

clean:
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	rm -f vpcs.db test_vpcs.db
	rm -rf .pytest_cache
