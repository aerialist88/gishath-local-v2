# Gishath Fetch v2 — Makefile
# Run all commands from the gishath-local-v2/ directory.

.PHONY: engine-build run sync-engine clean install-playwright atelier atelier-web gallery gallery-preview gallery-deploy ck-refresh watchlist-check iphone help

GO          = /usr/local/go/bin/go
ENGINE_SRC  = engine-src/api
ENGINE_BIN  = $(shell pwd)/bin/gishath-engine

help:
	@echo ""
	@echo "  make engine-build       Build the Go engine binary → bin/gishath-engine"
	@echo "  make install-playwright Install Playwright + Chromium browser"
	@echo "  make run                Start the Flask app (engine + Playwright auto-start)"
	@echo "  make atelier            The Deckwright's Atelier — native desktop window"
	@echo "  make atelier-web        The Atelier in the browser (http://127.0.0.1:5077)"
	@echo "  make gallery            Bake the public read-only gallery → gallery_site/"
	@echo "  make gallery-preview    Bake, then serve it locally (http://127.0.0.1:5099)"
	@echo "  make gallery-deploy     Bake + push the gallery to GitHub Pages"
	@echo "  make sync-engine        Pull upstream fixes into the engine fork"
	@echo "  make ck-refresh         Refresh the Card Kingdom reference-price cache (MTGJSON)"
	@echo "  make watchlist-check    Run the price-watch alert check now (also runs nightly)"
	@echo "  make iphone             Build the native iOS app + install to the iPhone (re-signs the 7-day cert)"
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

atelier:
	. venv/bin/activate && python -m atelier.desktop

atelier-web:
	. venv/bin/activate && python -m atelier.server

gallery:
	. venv/bin/activate && python -m atelier.publish

gallery-preview: gallery
	. venv/bin/activate && cd gallery_site && python -m http.server 5099

gallery-deploy:
	./publish_gallery.sh

ck-refresh:
	. venv/bin/activate && python refresh_ck_prices.py

watchlist-check:
	. venv/bin/activate && python check_watchlist.py

# Also refreshes the launchd mirror (launchd can't read ~/Desktop — TCC),
# so the nightly cert-renewal job always builds the code you last deployed.
IOS_MIRROR = $(HOME)/Library/Application Support/ThreevorFetch/repo
iphone:
	mkdir -p "$(IOS_MIRROR)"
	rsync -a --delete --exclude .DS_Store ios/ "$(IOS_MIRROR)/ios/"
	./ios/deploy.sh

sync-engine:
	@echo "Fetching upstream changes..."
	cd "$(ENGINE_SRC)/.." && git fetch origin && git merge origin/master
	@echo "✓  Synced. Run 'make engine-build' to rebuild."
	@echo "Note: api/cmd/serve/ (the local HTTP shim) is untracked here — merge won't touch it."

clean:
	rm -f "$(ENGINE_BIN)"
	@echo "✓  Cleaned."
