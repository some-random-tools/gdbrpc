# gdbrpc — development helpers
PYTHON ?= python3
VENV := .venv/bin

.PHONY: install update status test lint clean

## install: create the venv and install everything (idempotent)
install:
	./install.sh

## update: re-install the current code into the venv
update:
	./install.sh --update

## status: sanity-check the installation
status:
	$(VENV)/gdbrpc status

## test: end-to-end test against a real gdb (safe to run repeatedly;
## uses its own port range, kills its gdb afterwards)
test:
	$(VENV)/python tests/e2e.py

## lint: pyflakes over runtime + plugins + tests + dashboard
lint:
	$(VENV)/python -m pyflakes src/gdbrpc plugins tests/e2e.py \
		$(HOME)/scratch/mps-ui/user_plugins/gdbrpc_dashboard.py

## clean: venv + build artifacts + test leftovers
clean:
	rm -rf .venv build dist src/*.egg-info
	rm -f tests/e2e.log tests/e2e2.log tests/target