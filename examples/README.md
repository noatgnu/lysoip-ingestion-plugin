# Examples

Sample input files for manual testing and for CI.

`params.json` holds the `--param`/`--params-json` values `cauldron job run` uses against this
plugin's inputs (see `plugin.yaml`'s `inputs:` section, or run
`cauldron plugin inputs lysoip-ingestion` once installed). CI runs this automatically via
`.github/workflows/test-plugin.yml`.

```json
{
  "format": "diann",
  "raw_file": "examples/diann_pg_matrix.tsv",
  "peptide_file": "examples/diann_pr_matrix.tsv",
  "sample_sheet_file": "examples/diann_sample_sheet.csv"
}
```
