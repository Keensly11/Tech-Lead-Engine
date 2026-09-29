# Evaluation

`sample_signals.json`: fictional companies used for demos and `tests/test_pipeline.py`.

## Week 2: scorer precision
1. Run the pipeline on real sources for a week.
2. Export the top 100 leads to `labels.csv` with columns `company_id,company,good_lead` and
   have a salesperson mark each `1` (worth contacting) or `0`.
3. Measure precision@20 (what share of the top 20 are good) and compare weight changes in
   `config/scoring.yaml` against it. Keep the labels file out of git if it contains real data.
