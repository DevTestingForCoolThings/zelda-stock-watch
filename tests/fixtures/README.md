# Test fixtures

Minimised copies of real store pages and API responses, captured once and trimmed
to the fields the checkers read. Tests replay these instead of contacting real
stores, so running the test suite never touches anyone's rate limits.

Pages that list related products also carry *decoy* products with the opposite
stock status, so the tests prove each checker reads the product in the link and
not whichever product happens to come first or last.

| Fixture | Source | What it shows |
|---|---|---|
| `nintendo_ca_instock.html` | https://www.nintendo.com/en-ca/store/products/nintendo-switch-2-pro-controller-123674/ | InStock; isSalableQty=True; 2 decoy related products; server HTML had 'Find retailers'=True, 'Add to cart'=False |
| `nintendo_ca_soldout.html` | https://www.nintendo.com/en-ca/store/products/nintendo-switch-2-pro-controller-the-legend-of-zelda-40th-anniversary-edition-127074/ | OutOfStock; isSalableQty=False; 2 decoy related products; server HTML had 'Find retailers'=True, 'Add to cart'=False |
| `nintendo_ca_preorder.html` | https://www.nintendo.com/en-ca/store/products/nintendo-switch-2-carrying-case-screen-protector-the-legend-of-zelda-40th-anniversary-edition-127073/ | InStock; isSalableQty=True; 2 decoy related products; server HTML had 'Find retailers'=True, 'Add to cart'=False |
| `nintendo_us_instock.html` | https://www.nintendo.com/us/store/products/nintendo-switch-2-pro-controller-123674/ | InStock; isSalableQty=True; 2 decoy related products; server HTML had 'Find retailers'=True, 'Add to cart'=False |
| `nintendo_us_soldout.html` | https://www.nintendo.com/us/store/products/nintendo-switch-2-pro-controller-display-stand-the-legend-of-zelda-40th-anniversary-edition-127076/ | OutOfStock; isSalableQty=False; 2 decoy related products; server HTML had 'Find retailers'=True, 'Add to cart'=False |
| `bestbuy_ca_instock.json` | https://www.bestbuy.ca/ecomm-api/availability/products?accept-language=en-CA&skus=19523671 | availability API response, shipping InStock (Nintendo Switch 2 Pro Controller) |
| `bestbuy_ca_soldout.json` | https://www.bestbuy.ca/ecomm-api/availability/products?accept-language=en-CA&skus=20149830 | availability API response, SoldOutOnline (Zelda 40th Pro Controller) |
| `amazon_ca_instock.html` | https://www.amazon.ca/dp/B0FCYL8GG3 | availability 'In Stock'; add-to-cart=True; outOfStock box=False |
| `amazon_ca_soldout.html` | https://www.amazon.ca/dp/B0HJ6F8L6V | availability 'Currently unavailable.'; add-to-cart=False; outOfStock box=True |
| `amazon_com_instock.html` | https://www.amazon.com/dp/B0G1ZD8289 | availability 'In Stock'; add-to-cart=True; outOfStock box=False |
| `walmart_ca_instock.html` | https://www.walmart.ca/en/ip/Nintendo-Switch-2-The-Legend-of-Zelda-40th-Anniversary-Edition/4LPRUXHD1MMQ | IN_STOCK sold by Walmart; one OUT_OF_STOCK decoy recommendation |
| `walmart_ca_soldout.html` | https://www.walmart.ca/en/ip/Nintendo-Switch-2-Pro-Controller-The-Legend-of-Zelda-40th-Anniversary-Edition/3CVEAW67GHIT | OUT_OF_STOCK sold by Walmart; one IN_STOCK decoy recommendation |
| `walmart_com_soldout.html` | https://www.walmart.com/ip/Nintendo-Switch-2-Pro-Controller-The-Legend-of-Zelda-40th-Anniversary-Edition/20954470204 | OUT_OF_STOCK sold by Walmart.com; one IN_STOCK decoy recommendation |
| `ebgames_ca_soldout.html` | https://www.ebgames.ca/shop/nintendo-switch-2-the-legend-of-zelda-40th-anniversary-edition-220621 | OutOfStock $709.99 |
| `shopify_instock.js.json` | https://www.limitedrungames.com/products/r-type-dx-cd-soundtrack-limited-run-games-edition.js | Shopify product JSON; available=True; 1 variant(s) |
| `shopify_soldout.js.json` | https://www.limitedrungames.com/products/switch-limited-run-300-capcom-arcade-stadium-vol-1-event-exclusive.js | Shopify product JSON; available=False; 1 variant(s) |
| `state_v1.json` | state.json on origin/main, 2026-09-13 | the live bot's real version-1 state (targets keyed by id), for the migration test |

To refresh them, capture new pages and rebuild; keep one in-stock and one
sold-out example per store.
