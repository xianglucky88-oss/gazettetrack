PY=python3
run:
	PYTHONPATH=src $(PY) -m gazettetrack.cli
test:
	PYTHONPATH=src $(PY) -m pytest -q
test-stdlib:
	PYTHONPATH=. $(PY) tests/attach_runner.py
clean:
	rm -rf .pytest_cache **/__pycache__ *.db archive
