PYTHON      ?= python3
VENV        := .venv
PY          := $(VENV)/bin/python
PIP         := $(VENV)/bin/pip
PYTEST      := $(VENV)/bin/pytest
PYFLAKES    := $(VENV)/bin/pyflakes

SCRIPT      := main.py

# Параметры запуска (переопределяются: make run-udp UDP_PORT=6000)
UDP_HOST    ?= 0.0.0.0
UDP_PORT    ?= 5555
SERIAL_DEV  ?= /dev/ttyUSB0
BAUD        ?= 420000
UNIX_SOCK   ?= /tmp/crsf.sock

.PHONY: help venv deps run run-udp run-serial run-stdin run-unix-dgram run-unix-stream test lint clean distclean

help:
	@echo "Targets:"
	@echo "  make deps            — создать venv и поставить зависимости (pyserial, pytest, pyflakes)"
	@echo "  make run-udp         — запуск, слушает UDP  (UDP_HOST=$(UDP_HOST) UDP_PORT=$(UDP_PORT))"
	@echo "  make run-serial      — запуск с UART напрямую (SERIAL_DEV=$(SERIAL_DEV) BAUD=$(BAUD))"
	@echo "  make run-stdin       — запуск в режиме --stdin (для tee-пайплайна на клиенте)"
	@echo "  make run-unix-dgram  — бинд UNIX datagram-сокета (UNIX_SOCK=$(UNIX_SOCK))"
	@echo "  make run-unix-stream — клиент к существующему UNIX stream-сокету (UNIX_SOCK=$(UNIX_SOCK))"
	@echo "  make test            — прогнать pytest (tests/)"
	@echo "  make lint            — py_compile + pyflakes"
	@echo "  make clean           — удалить __pycache__/*.pyc"
	@echo "  make distclean       — clean + удалить venv"

# --- окружение -------------------------------------------------------------

venv: $(VENV)/bin/activate

$(VENV)/bin/activate:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip

deps: venv
	$(PIP) install pyserial pytest pyflakes

# --- запуск ------------------------------------------------------------

run: run-udp

run-udp: venv
	$(PY) $(SCRIPT) --udp $(UDP_HOST):$(UDP_PORT)

run-serial: venv
	$(PY) $(SCRIPT) --serial $(SERIAL_DEV) --baud $(BAUD)

run-stdin: venv
	@echo "Пример клиентского пайплайна:"
	@echo "  cat $(SERIAL_DEV) | tee >($(PY) $(SCRIPT) --stdin) | main_consumer"
	$(PY) $(SCRIPT) --stdin

run-unix-dgram: venv
	$(PY) $(SCRIPT) --unix-dgram $(UNIX_SOCK)

run-unix-stream: venv
	$(PY) $(SCRIPT) --unix-stream $(UNIX_SOCK)

# --- тесты / качество кода --------------------------------------------

test: deps
	$(PYTEST) -q tests/

lint: deps
	$(PY) -m py_compile $(SCRIPT)
	-$(PYFLAKES) $(SCRIPT) tests/

# --- уборка --------------------------------------------------------------

clean:
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find . -name '*.pyc' -delete

distclean: clean
	rm -rf $(VENV)
