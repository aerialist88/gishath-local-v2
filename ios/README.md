# 3vor Fetch — native iPhone app

A fully standalone SwiftUI port of the multi-card price scraper. All 15
Singapore LGS stores are scraped **on the phone** — no Mac, no Flask, no
tunnel needed once the app is installed.

## How it maps to the Mac stack

| Mac stack | iPhone app |
|---|---|
| Playwright headless Chromium (10 Cloudflare/BinderPOS stores) | Hidden `WKWebView` pool running the **same extraction JS** (`WebStores.swift`) |
| Go engine goquery scrapers (Dueller's Point, 5 Mana) | Same WKWebView path, new variant-5/6 extraction JS |
| Go engine JSON APIs (Cards & Collections, Mox & Lotus, TCG Marketplace) | `URLSession` ports (`APIStores.swift`), incl. the RSA-OAEP search deep link |
| filters.py (name match, non-MTG filter, quality/foil) | `Filters.swift` — keep in sync when filters.py changes |
| app.py merge → rank → +S$0.40 TCG landed cost | `SearchModel.swift` |
| Saved lists (state/buy_lists.json) | On-device (UserDefaults), seeded from `SeedLists.json` snapshot |
| Excel download | CSV share sheet |

Not ported (stays on the Mac): watchlist nightly emails, CK (US) reference
prices, price history, collection.

## One-time setup

1. Install **Xcode** from the Mac App Store (free), then:
   `sudo xcode-select -s /Applications/Xcode.app/Contents/Developer`
2. Open `ios/ThreevorFetch.xcodeproj`, select the ThreevorFetch target →
   **Signing & Capabilities** → Team: your **Personal Team** (free Apple ID).
3. Phone: enable **Developer Mode** (Settings → Privacy & Security), plug in
   via USB once so the Mac pairs it (Xcode → Window → Devices and Simulators).
4. Build & run to the phone from Xcode once; on the phone trust the cert
   under Settings → General → VPN & Device Management.

## Every week after (free Apple ID = 7-day cert)

```
make iphone
```

Rebuilds, re-signs, reinstalls over USB or Wi-Fi (`xcrun devicectl`), and
relaunches. Saved lists and app data survive reinstalls.
