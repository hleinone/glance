# Ideas

Maybes for later, not planned work (that's under "Next steps" in `CLAUDE.md`). Ideas that name the household's own services go in `IDEAS.local.md` (git-ignored). When an idea gets built, take it out of here: what was built, and why, goes in `CLAUDE.md`.

## New things to show

- Moon icon for clear nights.
- Sunrise/sunset.
- Calendar.
- Voice assistant.
- Other regions and sources, as new widget types: `waltti_stops` (Digitransit's Waltti region, sharing the provider and key with `hsl_stops`), `foli_stops`, `openmeteo_weather_now`. Types that look alike would share drawing helpers (departure row, route badge, hourly column) in `draw.py`.

## Layout and widgets

Left over from the configurable layout work (screens, decks, buttons and widgets that fill their area were built 2026-11-12):

- Light status bar: black text on white, as an option (`[status_bar] style = "light"`). The bar stays a fixed strip outside the grid, because the firmware draws its offline and refresh marks over the bar's right end; those marks are white badges made for a black bar, so they need a second look too.
- Smaller designed versions of widgets: 1×1 or half-height current weather (drop wind, then "feels like", a smaller icon), narrow stops without destinations.
- Widgets that use a taller area: current weather and the hourly forecast leave the space below them empty.
- Limits in the config: `max_departures`, `hours`, an hourly `step` (every 3 hours).
- A `size = "large"` preset for screens read across the room: one more tested size step, with the weather icons pre-rendered at that size too.
- A Mac-only contact-sheet tool that draws each widget at a range of sizes, to design the above.
