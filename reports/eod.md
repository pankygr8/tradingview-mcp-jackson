# EOD scorecard — 2026-10-23 21:22 IST

## Live results by date

| session    |   signals |   trades |   success |   failed |   success% |   total_R |   avg_R |   open |   no_fill |   bad_plan |   pending |
|:-----------|----------:|---------:|----------:|---------:|-----------:|----------:|--------:|-------:|----------:|-----------:|----------:|
| 2026-10-23 |       213 |       70 |        31 |       39 |       44.3 |      1.45 |   0.021 |      0 |        33 |          2 |       108 |
| 2026-10-22 |       189 |       77 |        27 |       50 |       35.1 |    -21.26 |  -0.276 |     77 |        32 |          3 |         0 |
| 2026-10-21 |       238 |       71 |        12 |       59 |       16.9 |    -43.52 |  -0.613 |    155 |        11 |          1 |         0 |

## 2026-10-23 by scanner

| session    | scanner     | bucket         |   signals |   trades |   success |   failed |   success% |   total_R |   avg_R |   open |   no_fill |   bad_plan |   pending |
|:-----------|:------------|:---------------|----------:|---------:|----------:|---------:|-----------:|----------:|--------:|-------:|----------:|-----------:|----------:|
| 2026-10-23 | ath         | half_ath_above |         8 |        0 |         0 |        0 |      nan   |      0    | nan     |      0 |         0 |          0 |         8 |
| 2026-10-23 | ath         | half_ath_below |        14 |        0 |         0 |        0 |      nan   |      0    | nan     |      0 |         0 |          0 |        14 |
| 2026-10-23 | gann_fan_1d | buy            |         9 |        0 |         0 |        0 |      nan   |      0    | nan     |      0 |         0 |          0 |         9 |
| 2026-10-23 | gann_fan_1d | buy_confirmed  |         4 |        0 |         0 |        0 |      nan   |      0    | nan     |      0 |         0 |          0 |         4 |
| 2026-10-23 | gann_fan_1d | sell           |        11 |        0 |         0 |        0 |      nan   |      0    | nan     |      0 |         0 |          0 |        11 |
| 2026-10-23 | gann_fan_1d | sell_confirmed |         2 |        0 |         0 |        0 |      nan   |      0    | nan     |      0 |         0 |          0 |         2 |
| 2026-10-23 | intraday    | arming         |         2 |        2 |         1 |        1 |       50   |      0.55 |   0.277 |      0 |         0 |          0 |         0 |
| 2026-10-23 | momentum    | actionable     |        34 |       32 |        16 |       16 |       50   |     -1.42 |  -0.044 |      0 |         0 |          2 |         0 |
| 2026-10-23 | momentum    | arming         |        69 |       36 |        14 |       22 |       38.9 |      2.31 |   0.064 |      0 |        33 |          0 |         0 |
| 2026-10-23 | swing       | breakouts      |         5 |        0 |         0 |        0 |      nan   |      0    | nan     |      0 |         0 |          0 |         5 |
| 2026-10-23 | swing       | coiled         |        12 |        0 |         0 |        0 |      nan   |      0    | nan     |      0 |         0 |          0 |        12 |
| 2026-10-23 | swing       | pullback       |        14 |        0 |         0 |        0 |      nan   |      0    | nan     |      0 |         0 |          0 |        14 |
| 2026-10-23 | swing       | time_cycle     |        20 |        0 |         0 |        0 |      nan   |      0    | nan     |      0 |         0 |          0 |        20 |
| 2026-10-23 | swing       | tradeable      |         5 |        0 |         0 |        0 |      nan   |      0    | nan     |      0 |         0 |          0 |         5 |
| 2026-10-23 | swing       | tradeable_time |         4 |        0 |         0 |        0 |      nan   |      0    | nan     |      0 |         0 |          0 |         4 |

## Tuning flags (conditions that separate winners from losers)

| scanner    | bucket          | feature         | condition               |   trades |   share% |   win% |   avg_R |   base_win% |   base_avg_R |   live_trades |   t_vs_rest |   lift_R |   lift_win% | suggest        |
|:-----------|:----------------|:----------------|:------------------------|---------:|---------:|-------:|--------:|------------:|-------------:|--------------:|------------:|---------:|------------:|:---------------|
| ath        | half_ath_above  | signal_hour     | signal_hour = 16        |       11 |     12.9 |    9.1 |  -0.727 |        47.1 |        0.004 |            11 |       -2.67 |   -0.731 |       -38   | FILTER OUT     |
| momentum   | actionable      | range_expansion | range_expansion <= 0.88 |       19 |     34.5 |   42.1 |  -0.247 |        45.5 |       -0.097 |            19 |       -1.8  |   -0.15  |        -3.4 | FILTER OUT     |
| momentum   | actionable      | rr_tp1          | rr_tp1 >= 1.66          |       18 |     32.7 |   33.3 |  -0.351 |        45.5 |       -0.097 |            18 |       -2.95 |   -0.254 |       -12.2 | FILTER OUT     |
| momentum   | arming          | score           | score >= 51.93          |       26 |     33.3 |   19.2 |  -0.3   |        35.9 |       -0.112 |            26 |       -3.15 |   -0.188 |       -16.7 | FILTER OUT     |
| price_time | pt_continuation | turned          | turned = True           |       14 |     19.7 |   64.3 |   0.454 |        46.5 |        0.118 |             0 |        1.92 |    0.336 |        17.8 | KEEP only this |
| price_time | pt_continuation | pt_ratio        | pt_ratio >= 0.957       |       24 |     33.8 |   50   |   0.409 |        46.5 |        0.118 |             0 |        1.53 |    0.291 |         3.5 | KEEP only this |
| price_time | pt_continuation | units_moved     | units_moved >= 6.167    |       24 |     33.8 |   54.2 |   0.387 |        46.5 |        0.118 |             0 |        1.72 |    0.269 |         7.7 | KEEP only this |
| price_time | pt_continuation | bars_from_pivot | bars_from_pivot >= 6    |       20 |     28.2 |   60   |   0.386 |        46.5 |        0.118 |             0 |        1.66 |    0.268 |        13.5 | KEEP only this |
| price_time | pt_continuation | unit_pct        | unit_pct <= 1.969       |       24 |     33.8 |   50   |   0.366 |        46.5 |        0.118 |             0 |        1.55 |    0.248 |         3.5 | KEEP only this |
| price_time | pt_reversal     | pt_gap          | pt_gap 0.48 – 0.71      |       22 |     31   |   31.8 |  -0.349 |        43.7 |        0.015 |             0 |       -1.79 |   -0.364 |       -11.9 | FILTER OUT     |
| swing      | breakouts       | rr              | rr 1.14 – 1.37          |       58 |     33.1 |   63.8 |   0.336 |        50.3 |        0.089 |             0 |        2.39 |    0.247 |        13.5 | KEEP only this |
| swing      | breakouts       | rs_63d          | rs_63d -2.58 – 5.2      |       58 |     33.1 |   62.1 |   0.28  |        50.3 |        0.089 |             1 |        2.01 |    0.191 |        11.8 | KEEP only this |
| swing      | breakouts       | score           | score <= 3.11           |       59 |     33.7 |   57.6 |   0.262 |        50.3 |        0.089 |             3 |        2.02 |    0.173 |         7.3 | KEEP only this |
| swing      | tradeable       | rs_63d          | rs_63d >= 9.55          |       64 |     33.2 |   60.9 |   0.468 |        45.6 |        0.109 |             1 |        3.13 |    0.359 |        15.3 | KEEP only this |
| swing      | tradeable       | score           | score >= 16.46          |       64 |     33.2 |   53.1 |   0.326 |        45.6 |        0.109 |             0 |        1.92 |    0.217 |         7.5 | KEEP only this |
| swing      | tradeable       | rvol_daily      | rvol_daily <= 0.62      |       65 |     33.7 |   52.3 |   0.276 |        45.6 |        0.109 |             0 |        1.79 |    0.167 |         6.7 | KEEP only this |
| swing      | tradeable       | coil_ratio      | coil_ratio <= 0.303     |       65 |     33.7 |   52.3 |   0.264 |        45.6 |        0.109 |             0 |        1.72 |    0.155 |         6.7 | KEEP only this |
| swing      | tradeable_time  | score           | score >= 16.99          |       45 |     33.6 |   60   |   0.473 |        47   |        0.134 |             0 |        2.37 |    0.339 |        13   | KEEP only this |
| swing      | tradeable_time  | rs_63d          | rs_63d >= 9.867         |       45 |     33.6 |   57.8 |   0.41  |        47   |        0.134 |             0 |        2.35 |    0.276 |        10.8 | KEEP only this |
| swing      | tradeable_time  | coil_ratio      | coil_ratio <= 0.301     |       45 |     33.6 |   57.8 |   0.344 |        47   |        0.134 |             0 |        1.91 |    0.21  |        10.8 | KEEP only this |
| swing      | tradeable_time  | risk_pct        | risk_pct >= 3.783       |       45 |     33.6 |   48.9 |   0.287 |        47   |        0.134 |             1 |        1.69 |    0.153 |         1.9 | KEEP only this |