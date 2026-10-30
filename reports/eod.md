# EOD scorecard — 2026-10-30 22:33 IST

## Live results by date

| session    |   signals |   trades |   success |   failed |   success% |   total_R |   avg_R |   open |   no_fill |   bad_plan |   pending |
|:-----------|----------:|---------:|----------:|---------:|-----------:|----------:|--------:|-------:|----------:|-----------:|----------:|
| 2026-10-30 |       163 |       42 |        21 |       21 |       50   |     -4.18 |  -0.1   |      0 |        14 |          0 |       107 |
| 2026-10-29 |       142 |       52 |        25 |       27 |       48.1 |     -3.19 |  -0.061 |     75 |        15 |          0 |         0 |
| 2026-10-28 |       162 |       76 |        37 |       39 |       48.7 |      2.6  |   0.034 |     79 |         7 |          0 |         0 |
| 2026-10-27 |       206 |       99 |        50 |       49 |       50.5 |      5.29 |   0.053 |     86 |        21 |          0 |         0 |
| 2026-10-26 |       183 |       93 |        45 |       48 |       48.4 |     -3.25 |  -0.035 |     58 |        31 |          1 |         0 |
| 2026-10-23 |       213 |      116 |        43 |       73 |       37.1 |    -11.59 |  -0.1   |     50 |        44 |          3 |         0 |
| 2026-10-22 |       189 |      111 |        33 |       78 |       29.7 |    -34.96 |  -0.315 |     28 |        47 |          3 |         0 |
| 2026-10-21 |       238 |      136 |        44 |       92 |       32.4 |    -32.69 |  -0.24  |     70 |        31 |          1 |         0 |

## 2026-10-30 by scanner

| session    | scanner     | bucket         |   signals |   trades |   success |   failed |   success% |   total_R |   avg_R |   open |   no_fill |   bad_plan |   pending |
|:-----------|:------------|:---------------|----------:|---------:|----------:|---------:|-----------:|----------:|--------:|-------:|----------:|-----------:|----------:|
| 2026-10-30 | ath         | ath_90d        |         1 |        0 |         0 |        0 |      nan   |      0    | nan     |      0 |         0 |          0 |         1 |
| 2026-10-30 | ath         | half_ath_above |         6 |        0 |         0 |        0 |      nan   |      0    | nan     |      0 |         0 |          0 |         6 |
| 2026-10-30 | ath         | half_ath_below |        13 |        0 |         0 |        0 |      nan   |      0    | nan     |      0 |         0 |          0 |        13 |
| 2026-10-30 | fib_1d      | sell           |         1 |        0 |         0 |        0 |      nan   |      0    | nan     |      0 |         0 |          0 |         1 |
| 2026-10-30 | fib_1d      | sell_pt        |         1 |        0 |         0 |        0 |      nan   |      0    | nan     |      0 |         0 |          0 |         1 |
| 2026-10-30 | gann_fan_1d | buy            |         4 |        0 |         0 |        0 |      nan   |      0    | nan     |      0 |         0 |          0 |         4 |
| 2026-10-30 | gann_fan_1d | buy_confirmed  |         5 |        0 |         0 |        0 |      nan   |      0    | nan     |      0 |         0 |          0 |         5 |
| 2026-10-30 | gann_fan_1d | sell           |         8 |        0 |         0 |        0 |      nan   |      0    | nan     |      0 |         0 |          0 |         8 |
| 2026-10-30 | gann_fan_1d | sell_confirmed |         4 |        0 |         0 |        0 |      nan   |      0    | nan     |      0 |         0 |          0 |         4 |
| 2026-10-30 | momentum    | actionable     |        18 |       18 |        11 |        7 |       61.1 |     -2.17 |  -0.121 |      0 |         0 |          0 |         0 |
| 2026-10-30 | momentum    | arming         |        38 |       24 |        10 |       14 |       41.7 |     -2.01 |  -0.084 |      0 |        14 |          0 |         0 |
| 2026-10-30 | swing       | breakouts      |         9 |        0 |         0 |        0 |      nan   |      0    | nan     |      0 |         0 |          0 |         9 |
| 2026-10-30 | swing       | coiled         |        12 |        0 |         0 |        0 |      nan   |      0    | nan     |      0 |         0 |          0 |        12 |
| 2026-10-30 | swing       | pullback       |        20 |        0 |         0 |        0 |      nan   |      0    | nan     |      0 |         0 |          0 |        20 |
| 2026-10-30 | swing       | time_cycle     |        17 |        0 |         0 |        0 |      nan   |      0    | nan     |      0 |         0 |          0 |        17 |
| 2026-10-30 | swing       | tradeable      |         5 |        0 |         0 |        0 |      nan   |      0    | nan     |      0 |         0 |          0 |         5 |
| 2026-10-30 | swing       | tradeable_time |         1 |        0 |         0 |        0 |      nan   |      0    | nan     |      0 |         0 |          0 |         1 |

## Tuning flags (conditions that separate winners from losers)

| scanner     | bucket          | feature         | condition               |   trades |   share% |   win% |   avg_R |   base_win% |   base_avg_R |   live_trades |   t_vs_rest |   lift_R |   lift_win% | suggest        |
|:------------|:----------------|:----------------|:------------------------|---------:|---------:|-------:|--------:|------------:|-------------:|--------------:|------------:|---------:|------------:|:---------------|
| ath         | ath_90d         | signal_hour     | signal_hour = 16        |       56 |      2.8 |   17.9 |  -0.485 |        43.5 |        0.008 |            56 |       -3.1  |   -0.493 |       -25.6 | FILTER OUT     |
| ath         | half_ath_above  | pct_from_ath    | pct_from_ath <= -49     |       36 |     34   |   33.3 |  -0.5   |        39.6 |       -0.131 |             7 |       -1.53 |   -0.369 |        -6.3 | FILTER OUT     |
| ath         | half_ath_above  | dist_from_50pct | dist_from_50pct <= 1.99 |       36 |     34   |   33.3 |  -0.5   |        39.6 |       -0.131 |             7 |       -1.53 |   -0.369 |        -6.3 | FILTER OUT     |
| ath         | half_ath_above  | signal_hour     | signal_hour = 16        |       31 |     29.2 |    9.7 |  -0.71  |        39.6 |       -0.131 |            31 |       -4.38 |   -0.579 |       -29.9 | FILTER OUT     |
| ath         | half_ath_below  | signal_hour     | signal_hour = 16        |       14 |     17.9 |   14.3 |  -0.571 |        43.6 |        0.049 |            14 |       -1.96 |   -0.62  |       -29.3 | FILTER OUT     |
| gann_fan_1d | sell            | signal_hour     | signal_hour = 16        |       22 |      3.5 |    4.5 |  -0.855 |        39   |       -0.079 |            22 |       -5.86 |   -0.776 |       -34.5 | FILTER OUT     |
| momentum    | actionable      | side            | side = long             |       95 |     54   |   68.4 |   0.213 |        57.4 |        0.051 |            95 |        3    |    0.162 |        11   | KEEP only this |
| momentum    | actionable      | side            | side = short            |       81 |     46   |   44.4 |  -0.139 |        57.4 |        0.051 |            81 |       -1.99 |   -0.19  |       -13   | FILTER OUT     |
| momentum    | arming          | side            | side = long             |      117 |     52.7 |   56.4 |   0.241 |        47.3 |        0.06  |           117 |        3.01 |    0.181 |         9.1 | KEEP only this |
| momentum    | arming          | side            | side = short            |      105 |     47.3 |   37.1 |  -0.141 |        47.3 |        0.06  |           105 |       -1.87 |   -0.201 |       -10.2 | FILTER OUT     |
| price_time  | pt_continuation | turned          | turned = True           |       14 |     19.4 |   64.3 |   0.454 |        45.8 |        0.099 |             0 |        1.92 |    0.355 |        18.5 | KEEP only this |
| price_time  | pt_continuation | unit_pct        | unit_pct <= 1.994       |       24 |     33.3 |   50   |   0.366 |        45.8 |        0.099 |             0 |        1.55 |    0.267 |         4.2 | KEEP only this |
| price_time  | pt_reversal     | pt_gap          | pt_gap 0.48 – 0.71      |       22 |     31   |   31.8 |  -0.349 |        43.7 |        0.015 |             0 |       -1.79 |   -0.364 |       -11.9 | FILTER OUT     |
| swing       | breakouts       | rs_63d          | rs_63d -2.87 – 5.2      |       64 |     33.2 |   62.5 |   0.292 |        49.7 |        0.072 |             5 |        2.21 |    0.22  |        12.8 | KEEP only this |
| swing       | breakouts       | rr              | rr 1.14 – 1.36          |       63 |     32.6 |   61.9 |   0.267 |        49.7 |        0.072 |             7 |        1.98 |    0.195 |        12.2 | KEEP only this |
| swing       | breakouts       | score           | score <= 3.13           |       65 |     33.7 |   56.9 |   0.222 |        49.7 |        0.072 |             8 |        1.81 |    0.15  |         7.2 | KEEP only this |
| swing       | tradeable       | rs_63d          | rs_63d >= 8.843         |       69 |     33.5 |   59.4 |   0.449 |        44.7 |        0.091 |             3 |        3.05 |    0.358 |        14.7 | KEEP only this |
| swing       | tradeable       | score           | score >= 16.35          |       69 |     33.5 |   50.7 |   0.277 |        44.7 |        0.091 |             1 |        1.72 |    0.186 |         6   | KEEP only this |
| swing       | tradeable       | rvol_daily      | rvol_daily <= 0.63      |       71 |     34.5 |   52.1 |   0.262 |        44.7 |        0.091 |             3 |        1.78 |    0.171 |         7.4 | KEEP only this |
| swing       | tradeable       | coil_ratio      | coil_ratio <= 0.297     |       70 |     34   |   51.4 |   0.255 |        44.7 |        0.091 |             6 |        1.72 |    0.164 |         6.7 | KEEP only this |
| swing       | tradeable       | risk_pct        | risk_pct >= 3.757       |       69 |     33.5 |   50.7 |   0.248 |        44.7 |        0.091 |             5 |        1.79 |    0.157 |         6   | KEEP only this |
| swing       | tradeable_time  | rs_63d          | rs_63d >= 9.517         |       48 |     33.6 |   60.4 |   0.469 |        45.5 |        0.109 |             1 |        2.75 |    0.36  |        14.9 | KEEP only this |
| swing       | tradeable_time  | score           | score >= 16.71          |       48 |     33.6 |   58.3 |   0.427 |        45.5 |        0.109 |             0 |        2.24 |    0.318 |        12.8 | KEEP only this |
| swing       | tradeable_time  | risk_pct        | risk_pct >= 3.757       |       48 |     33.6 |   50   |   0.317 |        45.5 |        0.109 |             2 |        1.91 |    0.208 |         4.5 | KEEP only this |
| swing       | tradeable_time  | coil_ratio      | coil_ratio <= 0.294     |       48 |     33.6 |   54.2 |   0.268 |        45.5 |        0.109 |             4 |        1.52 |    0.159 |         8.7 | KEEP only this |
| swing       | tradeable_time  | score           | score <= 14.38          |       48 |     33.6 |   29.2 |  -0.233 |        45.5 |        0.109 |             7 |       -1.52 |   -0.342 |       -16.3 | FILTER OUT     |