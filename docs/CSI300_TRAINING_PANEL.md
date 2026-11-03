# CSI 300 point-in-time ML training panel

Place the provider-validated file at `runtime/research/csi300_pit_training.csv`. It is intentionally not committed. One row represents one historical CSI 300 constituent at one signal date.

Required columns:

```text
signal_date,label_end_date,thscode,in_csi300,forward_excess_252d,dupont_roe,cash_conversion,fcf_margin,gross_margin,revenue_growth_ttm_yoy,profit_growth_ttm_yoy,gross_margin_change_yoy,pe_ttm_approx,pb_approx,fcf_yield_approx,debt_asset_proxy,receivable_asset,accrual_abs_to_assets,volatility_1y,drawdown_1y
```

- `signal_date`: date when all features were public, `YYYY-MM-DD`.
- `label_end_date`: date when the 252-session forward return label matured. A row is trainable only after this date is strictly earlier than the model cutoff.
- `thscode`: provider identifier such as `600000.SH`.
- `in_csi300`: membership on `signal_date`, not current membership.
- `forward_excess_252d`: security's 252-session forward total return from next-session open, less the CSI 300 total return over the same window. Store decimal returns (e.g. `0.18` for 18%).
- Remaining columns are point-in-time factor features, using the same calculations and units as the baseline model. Leave unavailable features blank; do not fill them with zero.

The importer currently requires mature labels and rows marked as historical constituents. Ridge is always considered when scikit-learn is installed. LightGBM is optional and selects among eight documented configurations on the 2021–2023 validation window; the 2024 onward window is kept for out-of-sample comparison. The model never updates automatically or produces simulated-account orders.

Do not create this panel from today's CSI 300 constituents alone. Survivorship and future-membership leakage invalidate the comparison.
