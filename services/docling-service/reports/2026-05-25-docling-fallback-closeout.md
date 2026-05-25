# Docling Fallback Parser Closeout

Date: 2026-05-25

## Final fallback-parser status

Docling is finalized as the local fallback/review parser baseline for `--converter docling`. The current service-local policy keeps the user-facing interface simple while internally using quality-first conversion, Chinese OCR fallback, accurate table structure settings, referenced images, table/page/formula review crops, and local Granite-Docling-CodeFormula MLX when available.

The latest validation confirms that Docling output is now basically human-reviewable for the tested papers:

- Chinese text is readable after OCR fallback, with `/Gxx=0`.
- HTML image links remain valid.
- Tables remain available as structured artifacts plus table/page image crops for review.
- Formula placeholders are no longer left as bare dead ends. Converted formulas and undecoded placeholders now link to source/context crops when Docling exposes formula coordinates.

Docling is still not promoted to the final best parser for all paper intake. It is the local fallback baseline while broader parser research continues.

## Why EXO was not used

The Docling formula path remains local Granite-Docling-CodeFormula MLX through the service venv. EXO was not used because user testing showed poor compatibility for this functional model class. EXO currently fits chat-model workloads better than Docling's code/formula VLM path. The user also confirmed that `MinerU2.5-Pro-2604-1.2B-mlx` cannot be loaded or used properly through EXO, and MinerU work is explicitly outside this task.

## Formula traceability behavior

Before this closeout, `Formula not decoded` placeholders linked to high-resolution context crops, but successfully converted formulas only had evidence images in the review appendix. That made manual verification possible but not convenient near the relevant text.

This phase adds compact source/context links beside converted formulas whenever Docling provides formula coordinates and matching crops:

- `assets/formula_N.png` is linked as `source image`.
- `assets/formula_N_context.png` is linked as `context crop`.
- `Formula not decoded` links remain preserved.
- `metadata.json` and `status.json` now include `formula_source_link_count`.

Inline or text-interleaved formulas that Docling does not separate as formula items cannot receive per-formula crops. They remain a known Docling capability limitation and should be checked against page images or by a future parser.

## Final sample validation

Validation command shape:

```bash
PYTHONPATH=services/docling-service services/docling-service/.venv/bin/python -m docling_service.cli \
  --converter docling \
  --job-uuid <uuidv4> \
  --input-file-path <sample.pdf> \
  --output-root /tmp/docling-fallback-closeout
```

| Sample | Runtime | OCR | /Gxx | Formula model | Placeholders | Formula crops/context | Source links | Broken refs | Tables/crops | Assets |
| --- | ---: | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `CN.pdf` | 74.894s | true | 0 / 0.0 | `granite_docling_mlx` | 0 | 24 / 24 | 24 | 0 | 6 / 6 | 77 |
| `table-heavy-ai-table-transformer.pdf` | 16.968s | false | 0 / 0.0 | `granite_docling_mlx` | 0 | 1 / 1 | 1 | 0 | 5 / 5 | 25 |
| `two-col-arxiv-ai-gat.pdf` | 20.053s | false | 0 / 0.0 | `granite_docling_mlx` | 0 | 6 / 6 | 6 | 0 | 3 / 3 | 31 |

Observed warnings were expected quality-path signals:

- CN: `text_quality_failed_gxx_density; attempting OCR fallback`, `ocr_fallback_mac_used_after_gxx_quality_failure`, `table_structure_accurate_cell_matching_enabled`, `formula_enrichment_enabled_granite_docling_mlx`
- English samples: `table_structure_accurate_cell_matching_enabled`, `formula_enrichment_enabled_granite_docling_mlx`

Docling/Transformers still emitted stderr messages such as `Could not parse formula with MathML`, but the final HTML contained zero `Formula not decoded` occurrences in these validation outputs and all local HTML refs resolved.

## Cleanup performed

After preserving validation facts in this report, the following safe temporary validation roots were removed:

- `/tmp/docling-mlx-compare/`
- `/tmp/docling-mlx-final/`
- `/tmp/docling-fallback-closeout/`

No committed reports, service venv, model caches, source PDFs, or uncertain user files were deleted.

## Known limitations

- The user-reported missed CN formula 7 remains a manual-review caveat. If Docling does not detect a formula region, the service cannot create a per-formula source crop for it.
- Inline/text-interleaved formulas remain weaker than display/block formulas because Docling does not always separate them as formula items.
- Granite MLX improves practical local formula extraction and runtime, but it does not guarantee perfect LaTeX/MathML conversion.
- Table output is acceptable for the current fallback role, but table crops remain important for correction and review.
- The parser should not silently be treated as final intake truth; downstream use should inspect warning fields and review artifacts.

## Recommendation

Keep Docling as the local fallback parser baseline with Granite MLX enabled. Continue future parser research outside this task, especially for inline formula fidelity and missed formula-region detection. MinerU/Marker or a dedicated formula-region/inline parser should be evaluated as a separate next phase rather than through EXO.
