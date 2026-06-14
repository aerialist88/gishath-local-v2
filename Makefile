# Gishath Fetch v2 — Makefile
# Run all commands from the gishath-local-v2/ directory.

.PHONY: engine-build run sync-engine clean install-playwright help

GO          = /usr/local/go/bin/go
ENGINE_SRC  = ../gishath_local/gishathfetch-master/api
ENGINE_BIN  = $(shell pwd)/bin/gishath-engine

help:
	@echo ""
	@echo "  make engine-build       Build the Go engine binary → bin/gishath-engine"
	@echo "  make install-playwright Install Playwright + Chromium browser"
	@echo "  make run                Start the Flask app (engine + Playwright auto-start)"
	@echo "  make sync-engine        Pull upstream fixes into the engine fork"
	@echo "  make clean              Remove the built engine binary"
	@echo ""

install-playwright:
	. venv/bin/activate && pip install playwright && python -m playwright install chromium
	@echo "✓  Playwright + Chromium installed."

engine-build:
	@echo "Building gishath-engine..."
	@mkdir -p bin
	cd "$(ENGINE_SRC)" && "$(GO)" build -mod=vendor -o "$(ENGINE_BIN)" ./cmd/serve
	@echo "✓  Built: $(ENGINE_BIN)"

run:
	. venv/bin/activate && python app.py

sync-engine:
	@echo "Fetching upstream changes..."
	cd "$(ENGINE_SRC)/.." && git fetch upstream && git merge upstream/master
	@echo "✓  Synced. Run 'make engine-build' to rebuild."

clean:
	rm -f "$(ENGINE_BIN)"
	@echo "✓  Cleaned."
