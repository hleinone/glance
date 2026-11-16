# Test data

Real API responses, saved for the tests:

- `fmi_simple.xml`: an FMI open data forecast (WFS, "simple" format) for
  Pasila, Helsinki, saved 2026-11-09. Finnish Meteorological Institute open
  data, [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Source:
  Finnish Meteorological Institute.
- `hsl_departures.json`: departures from the Digitransit routing API for
  Pasilan asema (tram platforms 32 and 34, bus platforms 31 and 36, train
  platform 2), saved 2026-11-09. Helsinki Region Transport (HSL) open data,
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Source: HSL.

Made for the tests:

- `localhost.pem`: a self-signed certificate and its private key for
  127.0.0.1 and localhost, valid until 2126, so the tests can run a local
  HTTPS server. It protects nothing: the tests trust it explicitly.
