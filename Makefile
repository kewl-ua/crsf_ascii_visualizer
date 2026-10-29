PYTHON      ?= python3
VENV        ?= .venv
VENV_PYTHON := $(VENV)/bin/python

PREFIX      ?= /usr/local
DESTDIR     ?=
BINDIR      := $(PREFIX)/bin
LIBDIR      := $(PREFIX)/lib/crsf-ascii-visualizer
PKGDIR      := $(LIBDIR)/crsf_ascii_visualizer
MANDIR      := $(PREFIX)/share/man/man1

UDP_HOST    ?= 0.0.0.0
UDP_PORT    ?= 5555
SERIAL_DEV  ?= /dev/ttyUSB0
BAUD        ?= 420000
UNIX_SOCK   ?= /tmp/crsf.sock

.PHONY: help venv deps install uninstall check-prefix run run-udp run-serial run-stdin run-unix-dgram run-unix-stream test lint build clean distclean

help:
	@echo "Targets:"
	@echo "  make install         - install crsf-viz and its man page (PREFIX=$(PREFIX))"
	@echo "  make uninstall       - remove only files installed by make install"
	@echo "  make venv            - create a local development virtual environment"
	@echo "  make deps            - install the editable project, serial support and build tools in the venv"
	@echo "  make run-udp         - listen for UDP frames (UDP_HOST=$(UDP_HOST) UDP_PORT=$(UDP_PORT))"
	@echo "  make run-serial      - read UART (SERIAL_DEV=$(SERIAL_DEV) BAUD=$(BAUD))"
	@echo "  make run-stdin       - read raw CRSF bytes from stdin"
	@echo "  make run-unix-dgram  - bind a UNIX datagram socket (UNIX_SOCK=$(UNIX_SOCK))"
	@echo "  make run-unix-stream - connect to a UNIX stream server (UNIX_SOCK=$(UNIX_SOCK))"
	@echo "  make test            - run the standard-library unit tests"
	@echo "  make lint            - check Python syntax"
	@echo "  make build           - build the wheel and source distribution in dist/"
	@echo "  make clean           - remove generated Python bytecode"
	@echo "  make distclean       - clean and remove the default .venv directory"
	@echo "Use PYTHON=/path/to/python3 to select an interpreter."
	@echo "Use PREFIX=\$$HOME/.local for a user install; DESTDIR stages a package."

venv:
	@test -x "$(VENV_PYTHON)" || "$(PYTHON)" -m venv "$(VENV)"

deps: venv
	"$(VENV_PYTHON)" -m pip install --editable ".[serial]" build

check-prefix:
	@case "$(PREFIX)" in /*) ;; *) echo "PREFIX must be an absolute POSIX path." >&2; exit 1 ;; esac
	@case "$(DESTDIR)" in ""|/*) ;; *) echo "DESTDIR must be empty or an absolute POSIX path." >&2; exit 1 ;; esac

install: check-prefix
	@"$(PYTHON)" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "Python 3.10 or newer is required.")'
	install -d "$(DESTDIR)$(BINDIR)" "$(DESTDIR)$(PKGDIR)" "$(DESTDIR)$(MANDIR)"
	install -m 644 crsf_ascii_visualizer/__init__.py crsf_ascii_visualizer/__main__.py crsf_ascii_visualizer/cli.py "$(DESTDIR)$(PKGDIR)/"
	@{ printf '#!'; "$(PYTHON)" -c 'import sys; print(sys.executable)'; \
	  printf '%s\n' \
	    'from pathlib import Path' \
	    'import sys' \
	    'sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib" / "crsf-ascii-visualizer"))' \
	    'from crsf_ascii_visualizer.cli import run' \
	    'raise SystemExit(run())'; \
	} > "$(DESTDIR)$(BINDIR)/crsf-viz"
	chmod 755 "$(DESTDIR)$(BINDIR)/crsf-viz"
	install -m 644 man/crsf-viz.1 "$(DESTDIR)$(MANDIR)/crsf-viz.1"

uninstall: check-prefix
	rm -f -- "$(DESTDIR)$(BINDIR)/crsf-viz" "$(DESTDIR)$(MANDIR)/crsf-viz.1"
	rm -f -- "$(DESTDIR)$(PKGDIR)/__init__.py" "$(DESTDIR)$(PKGDIR)/__main__.py" "$(DESTDIR)$(PKGDIR)/cli.py"
	rm -f -- "$(DESTDIR)$(PKGDIR)"/__pycache__/__init__.*.pyc "$(DESTDIR)$(PKGDIR)"/__pycache__/__main__.*.pyc "$(DESTDIR)$(PKGDIR)"/__pycache__/cli.*.pyc
	@rmdir "$(DESTDIR)$(PKGDIR)/__pycache__" 2>/dev/null || :
	@rmdir "$(DESTDIR)$(PKGDIR)" 2>/dev/null || :
	@rmdir "$(DESTDIR)$(LIBDIR)" 2>/dev/null || :

run: run-udp

run-udp:
	"$(PYTHON)" -m crsf_ascii_visualizer --udp "$(UDP_HOST):$(UDP_PORT)"

run-serial:
	"$(PYTHON)" -m crsf_ascii_visualizer --serial "$(SERIAL_DEV)" --baud "$(BAUD)"

run-stdin:
	"$(PYTHON)" -m crsf_ascii_visualizer --stdin

run-unix-dgram:
	"$(PYTHON)" -m crsf_ascii_visualizer --unix-dgram "$(UNIX_SOCK)"

run-unix-stream:
	"$(PYTHON)" -m crsf_ascii_visualizer --unix-stream "$(UNIX_SOCK)"

test:
	"$(PYTHON)" -m unittest discover -s tests -v

lint:
	"$(PYTHON)" -m compileall -q crsf_ascii_visualizer main.py

build: deps
	"$(VENV_PYTHON)" -m build

clean:
	@find crsf_ascii_visualizer tests -type f -name '*.pyc' -delete
	@find crsf_ascii_visualizer tests -depth -type d -name '__pycache__' -empty -delete
	@rm -f -- __pycache__/main.*.pyc
	@rmdir __pycache__ 2>/dev/null || :

distclean: clean
	@case "$(VENV)" in .venv|./.venv) rm -rf -- "$(VENV)" ;; *) echo "Remove the custom VENV directory manually: $(VENV)" >&2; exit 1 ;; esac
