PYTHON      ?= python3
VENV        := .venv
PY          := $(VENV)/bin/python
PIP         := $(VENV)/bin/pip
PYTEST      := $(VENV)/bin/pytest
PYFLAKES    := $(VENV)/bin/pyflakes

SCRIPT      := main.py

# Run settings (override with: make run-udp UDP_PORT=6000)
UDP_HOST    ?= 0.0.0.0
UDP_PORT    ?= 5555
SERIAL_DEV  ?= /dev/ttyUSB0
BAUD        ?= 420000
UNIX_SOCK   ?= /tmp/crsf.sock

.PHONY: help venv deps run run-udp run-serial run-stdin run-unix-dgram run-unix-stream test lint clean distclean

help:
	@echo "Targets:"
	@echo "  make deps            - create a venv and install pyserial, pytest and pyflakes"
	@echo "  make run-udp         - listen for UDP frames (UDP_HOST=$(UDP_HOST) UDP_PORT=$(UDP_PORT))"
	@echo "  make run-serial      - read UART directly (SERIAL_DEV=$(SERIAL_DEV) BAUD=$(BAUD))"
	@echo "  make run-stdin       - read stdin for a client-side tee pipeline"
	@echo "  make run-unix-dgram  - bind a UNIX datagram socket (UNIX_SOCK=$(UNIX_SOCK))"
	@echo "  make run-unix-stream - connect to an existing UNIX stream socket (UNIX_SOCK=$(UNIX_SOCK))"
	@echo "  make test            - run pytest (tests/)"
	@echo "  make lint            - run py_compile and pyflakes"
	@echo "  make clean           - remove __pycache__ directories and .pyc files"
	@echo "  make distclean       - clean and remove the venv"

# --- environment -----------------------------------------------------------

venv: $(VENV)/bin/activate

$(VENV)/bin/activate:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip

deps: venv
	$(PIP) install pyserial pytest pyflakes

# --- run -------------------------------------------------------------------

run: run-udp

run-udp: venv
	$(PY) $(SCRIPT) --udp $(UDP_HOST):$(UDP_PORT)

run-serial: venv
	$(PY) $(SCRIPT) --serial $(SERIAL_DEV) --baud $(BAUD)

run-stdin: venv
	@echo "Example client pipeline:"
	@echo "  cat $(SERIAL_DEV) | tee >($(PY) $(SCRIPT) --stdin) | main_consumer"
	$(PY) $(SCRIPT) --stdin

run-unix-dgram: venv
	$(PY) $(SCRIPT) --unix-dgram $(UNIX_SOCK)

run-unix-stream: venv
	$(PY) $(SCRIPT) --unix-stream $(UNIX_SOCK)

# --- tests / code quality --------------------------------------------------

test: deps
	$(PYTEST) -q tests/

lint: deps
	$(PY) -m py_compile $(SCRIPT)
	-$(PYFLAKES) $(SCRIPT) tests/

# --- cleanup ---------------------------------------------------------------

clean:
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find . -name '*.pyc' -delete

distclean: clean
	rm -rf $(VENV)
