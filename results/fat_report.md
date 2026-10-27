# Factory acceptance test (simulated line, real OpenPLC runtime)

| Check | Result | Detail |
|---|---|---|
| I/O loss at power up is treated as E-stop, cleared only by RESET | PASS | PLC held E-stop until RESET |
| E-stop stops the belt | PASS | belt off after 30 ms |
| No restart without RESET after E-stop | PASS | belt stays off until reset |
| Jam detected and belt stopped | PASS | jam state after 2.9 s |
| Camera failure: parts rejected fail-safe | PASS | 6 parts rejected on timeout |
| Every inspected apple routed as the camera decided | PASS | 58/58 apples followed the camera verdict |

## Production over the run

| | |
|---|---|
| Apples finished | 65 |
| Inspected by the PLC | 68 |
| Bad apples that reached the good bin | 1 |
| Good apples rejected because the camera was offline (fail-safe) | 2 |
| Good apples rejected by a classifier mistake | 1 |
| Vision timeouts / jams / ejections (PLC counters) | 6 / 1 / 42 |
| Camera latency, trigger to result (ms) | p50 91, p95 104, max 107 (budget 400) |

## Event log

-   1.0 s: operator: START
-  30.0 s: E-STOP pressed
-  33.0 s: E-STOP released
-  34.0 s: operator: RESET
-  35.0 s: operator: START
-  45.0 s: fault injected: jam
-  45.0 s: apple jammed at ejector
-  55.0 s: jam cleared by operator
-  56.0 s: operator: RESET
-  57.0 s: operator: START
-  67.0 s: fault injected: camera offline
-  73.0 s: camera back online
-  85.0 s: operator: STOP
