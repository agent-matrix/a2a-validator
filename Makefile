# ====================================================================================
#
#   M A T R I X - A I  :::  C O N T R O L   P R O G R A M
#   "Know thyself."
#
#   Access programs with:  make help
#
# ====================================================================================

# System & Colors
BRIGHT_GREEN  := $(shell tput -T screen setaf 10)
DIM_GREEN     := $(shell tput -T screen setaf 2)
RESET         := $(shell tput -T screen sgr0)

# Python / Venv
SYS_PYTHON := python3
VENV_DIR   := .venv
PYTHON     := $(VENV_DIR)/bin/python
PIP        := $(PYTHON) -m pip

# App
APP_MODULE := app.main:app
PORT       := 7860

# Docker / HF Spaces
IMG_NAME   := a2a-validator:local
SPACE_URL  ?= https://huggingface.co/spaces/ruslanmv/a2a-validator

# Files & Dirs
REQ        := requirements.txt
TEST_DIR   := tests

.DEFAULT_GOAL := help

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------
help:
	@echo
	@echo "$(BRIGHT_GREEN)M A T R I X - A I ::: C O N T R O L   P R O G R A M$(RESET)"
	@echo
	@printf "$(BRIGHT_GREEN)  %-22s$(RESET) $(DIM_GREEN)%s$(RESET)\n" "PROGRAM" "DESCRIPTION"
	@printf "$(BRIGHT_GREEN)  %-22s$(RESET) $(DIM_GREEN)%s$(RESET)\n" "----------------------" "--------------------------------------------------------"
	@echo
	@echo "$(BRIGHT_GREEN)Environment$(RESET)"
	@printf "  $(BRIGHT_GREEN)%-22s$(RESET) $(DIM_GREEN)%s$(RESET)\n" "venv" "Create virtualenv (.venv)"
	@printf "  $(BRIGHT_GREEN)%-22s$(RESET) $(DIM_GREEN)%s$(RESET)\n" "install" "Install deps into venv (incremental)"
	@echo
	@echo "$(BRIGHT_GREEN)Quality$(RESET)"
	@printf "  $(BRIGHT_GREEN)%-22s$(RESET) $(DIM_GREEN)%s$(RESET)\n" "lint" "ruff check"
	@printf "  $(BRIGHT_GREEN)%-22s$(RESET) $(DIM_GREEN)%s$(RESET)\n" "fmt" "black + ruff fix"
	@printf "  $(BRIGHT_GREEN)%-22s$(RESET) $(DIM_GREEN)%s$(RESET)\n" "test" "pytest"
	@echo
	@echo "$(BRIGHT_GREEN)Run$(RESET)"
	@printf "  $(BRIGHT_GREEN)%-22s$(RESET) $(DIM_GREEN)%s$(RESET)\n" "run" "Run uvicorn (PORT=$(PORT))"
	@printf "  $(BRIGHT_GREEN)%-22s$(RESET) $(DIM_GREEN)%s$(RESET)\n" "run-hot" "Run with --reload"
	@echo
	@echo "$(BRIGHT_GREEN)Docker$(RESET)"
	@printf "  $(BRIGHT_GREEN)%-22s$(RESET) $(DIM_GREEN)%s$(RESET)\n" "docker-build" "Build local image ($(IMG_NAME))"
	@printf "  $(BRIGHT_GREEN)%-22s$(RESET) $(DIM_GREEN)%s$(RESET)\n" "docker-run" "Run local container (maps $(PORT))"
	@echo
	@echo "$(BRIGHT_GREEN)HF Spaces helpers$(RESET)"
	@printf "  $(BRIGHT_GREEN)%-22s$(RESET) $(DIM_GREEN)%s$(RESET)\n" "space-url" "Echo the Space URL (set SPACE_URL=...)"
	@echo

# ---------------------------------------------------------------------------
# Env
# ---------------------------------------------------------------------------
$(VENV_DIR)/bin/activate:
	@test -d $(VENV_DIR) || $(SYS_PYTHON) -m venv $(VENV_DIR)

venv: $(VENV_DIR)/bin/activate
	@echo "$(DIM_GREEN)-> Upgrading pip/setuptools/wheel$(RESET)"
	@$(PIP) install -U pip setuptools wheel >/dev/null

install: venv
	@echo "$(DIM_GREEN)-> Installing deps$(RESET)"
	@$(PIP) install -r $(REQ)
	@echo "$(BRIGHT_GREEN)OK$(RESET)"

# ---------------------------------------------------------------------------
# Quality
# ---------------------------------------------------------------------------
lint: venv
	@$(PYTHON) -m ruff check app tests || true

fmt: venv
	@$(PYTHON) -m black app tests || true
	@$(PYTHON) -m ruff check --fix app tests || true

test: venv
	@$(PYTHON) -m pytest -q --disable-warnings --maxfail=1 || true

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
run: install
	@PORT=$(PORT) $(VENV_DIR)/bin/uvicorn $(APP_MODULE) --host 0.0.0.0 --port $(PORT)

run-hot: install
	@PORT=$(PORT) $(VENV_DIR)/bin/uvicorn $(APP_MODULE) --host 0.0.0.0 --port $(PORT) --reload

# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------
docker-build:
	@docker build -t $(IMG_NAME) .

docker-run:
	@docker run --rm -it -p $(PORT):$(PORT) -e PORT=$(PORT) $(IMG_NAME)

# ---------------------------------------------------------------------------
# HF Helpers
# ---------------------------------------------------------------------------
space-url:
	@echo "Space: $(SPACE_URL)"

# ---------------------------------------------------------------------------
# Clean
# ---------------------------------------------------------------------------
clean:
	@rm -rf .venv __pycache__ .pytest_cache .ruff_cache .mypy_cache dist build *.egg-info

.PHONY: help venv install lint fmt test run run-hot kb kb-force docker-build docker-run space-url clean