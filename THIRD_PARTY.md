# Third-party material

This project's own code is MIT-licensed (see `LICENSE`). It also bundles or uses
the following, each under its own licence.

## Bundled files

| What | Where | Licence | Notes |
|---|---|---|---|
| **Inter** typeface 4.1, © 2016 The Inter Project Authors ([rsms/inter](https://github.com/rsms/inter)) | `glance-server/fonts/` | SIL Open Font License 1.1 (`fonts/LICENSE.txt`) | Unmodified static TTFs. |
| **FMI weather symbols** (WeatherSymbol3), © 2013 Finnish Meteorological Institute ([fmidev/opendata-resources](https://github.com/fmidev/opendata-resources)) | `glance-server/icons/fmi/` | MIT (`icons/fmi/LICENSE`) | `src/` holds the original SVGs (Illustrator private data removed); `36/` and `76/` are modified versions (greyscale, thicker lines, cropped) made by `tools/build_fmi_icons.py`. |
| **HSL transport mode pictograms**, © 2015 Finnish Transport Agency and Helsinki Regional Transport Authority HSL ([HSLdevcom/digitransit-ui](https://github.com/HSLdevcom/digitransit-ui)) | `glance-server/icons/hsl/` | EUPL-1.2 or AGPL-3.0 (`icons/hsl/COPYING`, `icons/hsl/LICENSE-EUPL.txt`) | `src/` holds the original SVGs; the PNGs are cropped and scaled by `tools/build_hsl_icons.py`. These files stay under their copyleft licence; the rest of the project is not derived from them. This project is not affiliated with or endorsed by HSL. |

## Data

The dashboard shows open data fetched at runtime, and the tests include saved
responses (`glance-server/tests/data/`, see the README there):

- **Weather:** Finnish Meteorological Institute open data, licensed under
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Source: Finnish
  Meteorological Institute (Ilmatieteen laitos). The tests also hold a few
  sunrise and sunset times FMI publishes (`glance-server/tests/test_sun.py`),
  as expected values for the server's own calculation.
- **Public transport:** Helsinki Region Transport (HSL) open data via the
  Digitransit API, licensed under
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Source: HSL.
- **Electricity prices:** Nord Pool's day-ahead market prices for Finland, as
  published by Elering (Estonia's transmission system operator) on its open
  dashboard API ([dashboard.elering.ee](https://dashboard.elering.ee/)), with no
  key. No licence or terms of use for this data were found on Elering's site.
  The tests use made-up answers in its shape, not saved ones. Source: Nord
  Pool, via Elering.

If you publish screenshots or other material showing this data, credit the
sources as above.

## Libraries (not bundled)

Installed or downloaded at build time, each under its own (permissive) licence:
Pillow (`glance-server`), and Seeed_GFX with the Espressif Arduino core
(`glance-device`, via PlatformIO). If you distribute compiled firmware, include
their licence notices with it.
