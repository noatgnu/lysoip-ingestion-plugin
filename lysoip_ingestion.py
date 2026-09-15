#!/usr/bin/env python3
"""Lyso-IP QC ingestion: converts one search-engine export into the tidy long-format
files every downstream lysoip-* plugin consumes (samples.tsv, abundance_long.tsv,
optionally peptide_abundance_long.tsv).

Ported from lysoip_qc_framework/apps/ingestion/adapters/{spectronaut,fragpipe,diann,
generic_tidy}.py, which are already pure pandas with zero Django dependency.
"""

import argparse
import csv
import sys
from pathlib import Path

import pandas as pd

SpectronautColumns = {
    "protein": "PG.ProteinGroups",
    "gene": "PG.Genes",
    "sample": "R.FileName",
    "quantity": "PG.Quantity",
    "peptide": "PEP.StrippedSequence",
    "peptide_quantity": "PEP.Quantity",
}

FragpipeColumns = {
    "protein": "Protein ID",
    "gene": "Gene",
    "intensity_suffix": " Intensity",
    "peptide": "Peptide Sequence",
}

DiannColumns = {
    "protein": "Protein.Group",
    "gene": "Genes",
    "peptide": "Stripped.Sequence",
}
DIANN_PG_FIXED_COLUMNS = {
    "Protein.Group", "Protein.Ids", "Protein.Names", "Genes",
    "First.Protein.Description", "N.Sequences", "N.Proteotypic.Sequences",
}
DIANN_PR_FIXED_COLUMNS = DIANN_PG_FIXED_COLUMNS | {
    "Proteotypic", "Stripped.Sequence", "Modified.Sequence", "Precursor.Charge", "Precursor.Id",
}

GenericTidyColumns = {"protein": "protein", "gene": "gene", "peptide": "peptide"}


def read_sample_sheet(path):
    """CSV/TSV with columns Sample, Group, ReplicateIndex -> {sample_name: (group, replicate_index)}."""
    table = pd.read_csv(path, sep=None, engine="python")
    required = {"Sample", "Group", "ReplicateIndex"}
    missing = required - set(table.columns)
    if missing:
        raise ValueError(f"Sample sheet missing required columns: {sorted(missing)}")

    sheet = {}
    for _, row in table.iterrows():
        group = str(row["Group"]).strip().lower()
        if group not in ("ip", "wcl"):
            raise ValueError(f"Sample sheet Group must be 'ip' or 'wcl', got {row['Group']!r} for sample {row['Sample']!r}")
        sheet[str(row["Sample"]).strip()] = (group, int(row["ReplicateIndex"]))
    return sheet


def build_samples(sample_names, sample_sheet):
    """Resolve raw sample identifiers against the sample sheet; extra unselected file
    columns are silently skipped, but every sample_sheet entry must appear in the file."""
    seen = list(dict.fromkeys(sample_names))
    selected = [name for name in seen if name in sample_sheet]
    missing = set(sample_sheet) - set(selected)
    if missing:
        raise ValueError(f"sample_sheet has entries not found in the file: {sorted(missing)}")
    return [{"sample": name, "group": sample_sheet[name][0], "replicate_index": sample_sheet[name][1]} for name in selected]


def parse_spectronaut(raw_path, sample_sheet):
    c = SpectronautColumns
    table = pd.read_csv(raw_path, sep="\t")
    required = {c["protein"], c["gene"], c["sample"], c["quantity"]}
    missing = required - set(table.columns)
    if missing:
        raise ValueError(f"Spectronaut report missing required columns: {sorted(missing)}")

    table = table[table[c["sample"]].isin(sample_sheet)]
    samples = build_samples(table[c["sample"]], sample_sheet)
    measurements = [
        {"sample": row[c["sample"]], "protein": row[c["protein"]], "gene": row[c["gene"]], "value": float(row[c["quantity"]])}
        for _, row in table.iterrows()
    ]
    return samples, measurements


def parse_spectronaut_peptides(raw_path, sample_sheet):
    c = SpectronautColumns
    table = pd.read_csv(raw_path, sep="\t")
    required = {c["peptide"], c["protein"], c["gene"], c["sample"], c["peptide_quantity"]}
    missing = required - set(table.columns)
    if missing:
        raise ValueError(f"Spectronaut peptide report missing required columns: {sorted(missing)}")

    table = table[table[c["sample"]].isin(sample_sheet)]
    samples = build_samples(table[c["sample"]], sample_sheet)
    measurements = [
        {
            "sample": row[c["sample"]], "peptide": row[c["peptide"]],
            "protein": row[c["protein"]], "gene": row[c["gene"]], "value": float(row[c["peptide_quantity"]]),
        }
        for _, row in table.iterrows()
    ]
    return samples, measurements


def parse_fragpipe(raw_path, sample_sheet):
    c = FragpipeColumns
    table = pd.read_csv(raw_path, sep="\t")
    if c["protein"] not in table.columns or c["gene"] not in table.columns:
        raise ValueError(f"FragPipe table missing required columns: {c['protein']!r}, {c['gene']!r}")

    suffix = c["intensity_suffix"]
    intensity_columns = {col: col[: -len(suffix)] for col in table.columns if col.endswith(suffix)}
    if not intensity_columns:
        raise ValueError("FragPipe table has no '<Sample> Intensity' columns")
    intensity_columns = {col: name for col, name in intensity_columns.items() if name in sample_sheet}

    samples = build_samples(intensity_columns.values(), sample_sheet)
    measurements = [
        {"sample": sample_name, "protein": row[c["protein"]], "gene": row[c["gene"]], "value": float(row[col])}
        for _, row in table.iterrows()
        for col, sample_name in intensity_columns.items()
    ]
    return samples, measurements


def parse_fragpipe_peptides(raw_path, sample_sheet):
    c = FragpipeColumns
    table = pd.read_csv(raw_path, sep="\t")
    missing = [col for col in (c["peptide"], c["protein"], c["gene"]) if col not in table.columns]
    if missing:
        raise ValueError(f"FragPipe peptide table missing required columns: {missing}")

    suffix = c["intensity_suffix"]
    intensity_columns = {col: col[: -len(suffix)] for col in table.columns if col.endswith(suffix)}
    if not intensity_columns:
        raise ValueError("FragPipe peptide table has no '<Sample> Intensity' columns")
    intensity_columns = {col: name for col, name in intensity_columns.items() if name in sample_sheet}

    samples = build_samples(intensity_columns.values(), sample_sheet)
    measurements = [
        {
            "sample": sample_name, "peptide": row[c["peptide"]],
            "protein": row[c["protein"]], "gene": row[c["gene"]], "value": float(row[col]),
        }
        for _, row in table.iterrows()
        for col, sample_name in intensity_columns.items()
    ]
    return samples, measurements


def _diann_representative_accession(protein_group):
    """Protein.Group is usually a single accession, but can be a ;-joined ambiguous group."""
    return protein_group.split(";")[0]


def parse_diann(raw_path, sample_sheet):
    c = DiannColumns
    table = pd.read_csv(raw_path, sep="\t")
    missing = [col for col in (c["protein"], c["gene"]) if col not in table.columns]
    if missing:
        raise ValueError(f"DIA-NN protein matrix missing required columns: {missing}")

    sample_columns = [col for col in table.columns if col not in DIANN_PG_FIXED_COLUMNS]
    if not sample_columns:
        raise ValueError("DIA-NN protein matrix has no sample columns")
    sample_columns = [col for col in sample_columns if col in sample_sheet]

    samples = build_samples(sample_columns, sample_sheet)
    measurements = [
        {
            "sample": sample_name, "protein": _diann_representative_accession(row[c["protein"]]),
            "gene": row[c["gene"]], "value": float(row[sample_name]),
        }
        for _, row in table.iterrows()
        for sample_name in sample_columns
        if pd.notna(row[sample_name])
    ]
    return samples, measurements


def parse_diann_peptides(raw_path, sample_sheet):
    c = DiannColumns
    table = pd.read_csv(raw_path, sep="\t")
    missing = [col for col in (c["peptide"], c["protein"], c["gene"]) if col not in table.columns]
    if missing:
        raise ValueError(f"DIA-NN peptide matrix missing required columns: {missing}")

    sample_columns = [col for col in table.columns if col not in DIANN_PR_FIXED_COLUMNS]
    if not sample_columns:
        raise ValueError("DIA-NN peptide matrix has no sample columns")
    sample_columns = [col for col in sample_columns if col in sample_sheet]

    samples = build_samples(sample_columns, sample_sheet)

    protein_by_peptide = {}
    sums = {}
    for _, row in table.iterrows():
        sequence = row[c["peptide"]]
        protein_by_peptide.setdefault(sequence, (_diann_representative_accession(row[c["protein"]]), row[c["gene"]]))
        for sample_name in sample_columns:
            value = row[sample_name]
            if pd.notna(value):
                key = (sequence, sample_name)
                sums[key] = sums.get(key, 0.0) + float(value)

    measurements = [
        {
            "sample": sample_name, "peptide": sequence,
            "protein": protein_by_peptide[sequence][0], "gene": protein_by_peptide[sequence][1], "value": value,
        }
        for (sequence, sample_name), value in sums.items()
    ]
    return samples, measurements


def parse_generic_tidy(raw_path, sample_sheet):
    c = GenericTidyColumns
    table = pd.read_csv(raw_path, sep=None, engine="python")
    missing = {c["protein"], c["gene"]} - set(table.columns)
    if missing:
        raise ValueError(f"Generic tidy table missing required columns: {sorted(missing)}")

    sample_columns = [col for col in table.columns if col not in {c["protein"], c["gene"]}]
    sample_columns = [col for col in sample_columns if col in sample_sheet]
    samples = build_samples(sample_columns, sample_sheet)
    measurements = [
        {"sample": sample_name, "protein": row[c["protein"]], "gene": row[c["gene"]], "value": float(row[sample_name])}
        for _, row in table.iterrows()
        for sample_name in sample_columns
    ]
    return samples, measurements


def parse_generic_tidy_peptides(raw_path, sample_sheet):
    c = GenericTidyColumns
    table = pd.read_csv(raw_path, sep=None, engine="python")
    missing = {c["peptide"], c["protein"], c["gene"]} - set(table.columns)
    if missing:
        raise ValueError(f"Generic tidy peptide table missing required columns: {sorted(missing)}")

    sample_columns = [col for col in table.columns if col not in {c["peptide"], c["protein"], c["gene"]}]
    sample_columns = [col for col in sample_columns if col in sample_sheet]
    samples = build_samples(sample_columns, sample_sheet)
    measurements = [
        {
            "sample": sample_name, "peptide": row[c["peptide"]],
            "protein": row[c["protein"]], "gene": row[c["gene"]], "value": float(row[sample_name]),
        }
        for _, row in table.iterrows()
        for sample_name in sample_columns
    ]
    return samples, measurements


PARSERS = {
    "spectronaut": (parse_spectronaut, parse_spectronaut_peptides),
    "fragpipe": (parse_fragpipe, parse_fragpipe_peptides),
    "diann": (parse_diann, parse_diann_peptides),
    "generic_tidy": (parse_generic_tidy, parse_generic_tidy_peptides),
}


def write_tsv(path, rows, fieldnames):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter="\t")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(description="Convert a search-engine export into tidy lysoip-* input files")
    parser.add_argument("--format", required=True, choices=sorted(PARSERS))
    parser.add_argument("--raw_file", required=True)
    parser.add_argument("--peptide_file", default=None)
    parser.add_argument("--sample_sheet_file", required=True)
    parser.add_argument("--output_folder", required=True)
    args = parser.parse_args()

    output_folder = Path(args.output_folder)
    output_folder.mkdir(parents=True, exist_ok=True)

    # @step: Reading sample sheet
    sample_sheet = read_sample_sheet(args.sample_sheet_file)

    # @step: Parsing protein-level abundance
    parse_fn, parse_peptides_fn = PARSERS[args.format]
    samples, measurements = parse_fn(args.raw_file, sample_sheet)

    write_tsv(output_folder / "samples.tsv", samples, ["sample", "group", "replicate_index"])
    write_tsv(output_folder / "abundance_long.tsv", measurements, ["sample", "protein", "gene", "value"])
    print(f"Wrote {len(samples)} samples, {len(measurements)} abundance measurements", file=sys.stderr)

    if args.peptide_file:
        # @step-if: Parsing peptide-level abundance
        peptide_samples, peptide_measurements = parse_peptides_fn(args.peptide_file, sample_sheet)
        write_tsv(
            output_folder / "peptide_abundance_long.tsv",
            peptide_measurements,
            ["sample", "peptide", "protein", "gene", "value"],
        )
        print(f"Wrote {len(peptide_measurements)} peptide abundance measurements", file=sys.stderr)

    # @step: Ingestion complete
    print("Ingestion complete.", file=sys.stderr)


if __name__ == "__main__":
    main()
