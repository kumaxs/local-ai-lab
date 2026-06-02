# CN Formula Route Feasibility Report

Date: 2026-06-02

Scope: `CN.pdf` formula extraction quality after Docling V1 parity. Route A is
the Docling Server standard-pipeline adapter. Route B remains evaluation-only
and was used only as a formula-quality reference, not as full-document
text/layout input.

## Conclusion

The current Route A Docling formula enrichment route should be replaced for
CN.pdf-style formula extraction. Adapter diagnostics and crops are useful, but
the actual formula text is systemically unreliable and was not materially fixed
by the practical Docling-native variants available locally.

The recommended next route is a formula-only second pass that keeps Route A for
OCR/text/layout/tables/review artifacts, but replaces formula text candidates
with a dedicated formula extraction strategy. A VLM-derived formula candidate
source is promising as a bounded formula-only pass, because it avoids the worst
Route A contamination on the reviewed formulas. It must not replace Route A
full-document text/layout unless separately approved, because the earlier Route
B review dropped right-column text and rendered pictures unreliably.

## Approaches Attempted

### Route A Baseline: Docling Server + Granite MLX

Output root:

```text
/Users/zeyuan/Projects/local-ai-lab/.runtime/review/docling-server-cn-formula-quality-2026-06-02-rerun/CN
```

Settings:

- Docling Server 1.20.0, Docling 2.95.0.
- Standard pipeline.
- OCRMac Chinese parity fallback.
- Accurate table mode.
- Granite-Docling-258M MLX formula enrichment.
- Adapter-owned review artifacts and formula source/context crops.

Results:

- Formula nodes: 24.
- Number-only formulas: 3.
- Formula `(3)`: not acceptable. The crop/provenance is visually plausible and
  stays in the left column, but the enriched formula text ends with
  `\text {所}`, indicating CJK/prose contamination from nearby text.
- Formula `(4)`: not acceptable. The final formula node is only `( 4 )`; the
  formula body is not recovered.
- Formula `(5)`: not acceptable. The provenance bbox is about 10 PDF points
  tall and covers only a fraction/separator line. Granite emits a repeated
  `\frac{\sqrt{d}}{\sqrt{d}}` chain, so both the source bbox and formula text
  are wrong for the visible formula body.
- Formula `(16)`: not acceptable. The formula text includes a long repeated
  `\ a n d` chain after the formula.

Assessment: not a crop-only issue. Formula `(3)` proves that even a plausible
formula crop can produce contaminated text. Formula `(5)` proves that Docling's
formula bbox can miss the body, making enrichment operate on the wrong visual
region.

### Formula Enrichment Off

Output root:

```text
/Users/zeyuan/Projects/local-ai-lab/.runtime/review/docling-cn-formula-variants-2026-06-02/CN_formula_off
```

Settings:

- Same Route A adapter and CN OCR parity policy.
- `--formula-policy off`.

Results:

- Formula nodes: 24.
- Number-only formulas: 0 by text count, because formula text is empty.
- Formula `(3)`: no contaminated text, but no usable formula text.
- Formula `(4)`: no usable formula text.
- Formula `(5)`: no usable formula text.
- Formula `(16)`: no repeated `and` contamination, but no usable formula text.

Assessment: useful as a fail-closed review mode, but not a material extraction
improvement. It avoids bad formula text by removing formula text.

### Docling Server Custom CodeFormulaV2

Output root:

```text
/Users/zeyuan/Projects/local-ai-lab/.runtime/review/docling-cn-formula-variants-2026-06-02/CN_codeformulav2
```

Settings:

- Docling Server standard pipeline.
- OCRMac Chinese parity fallback.
- Custom `CodeFormulaV2` formula config from installed Docling 2.95.0
  `CodeFormulaVlmOptions.from_preset("codeformulav2")`.

Results:

- Conversion did not complete.
- Runtime: about 2 seconds.
- Failure: HTTP 404 with Docling Serve detail
  `Task result not found. Please wait for a completion status.`

Assessment: not a practical Route A fix through the current Docling Server
execution path. The model files exist in the local mirror-style cache, but this
custom server run did not produce a conversion result.

### Local Docling API CodeFormulaV2

Output roots:

```text
/Users/zeyuan/Projects/local-ai-lab/.runtime/review/docling-cn-formula-variants-2026-06-02/CN_local_codeformulav2_ocrmac
/Users/zeyuan/Projects/local-ai-lab/.runtime/review/docling-cn-formula-variants-2026-06-02/CN_local_codeformulav2_localpath_ocrmac
```

Settings:

- Local Docling `DocumentConverter`.
- Standard PDF pipeline.
- OCRMac full-page OCR with `zh-Hans`, `zh-Hant`, and `en-US`.
- `CodeFormulaV2` formula enrichment.
- Second attempt overrode the model repo id with the local cache path:
  `/Users/zeyuan/.cache/docling/models/docling-project--CodeFormulaV2`.

Results:

- Both conversions failed before producing formula output.
- Failure:
  `LocalEntryNotFoundError('An error happened while trying to locate the file on the Hub and we cannot find the requested files in the local cache...')`

Assessment: not a practical local fix without changing dependency/cache
behavior or downloading/rearranging model assets. That is outside this bounded
adapter-quality task and still would not address Docling's formula-region
detection misses unless the model also changes localization behavior.

### Route B VLM Reference

Reference output root:

```text
/Users/zeyuan/Projects/local-ai-lab/.runtime/review/docling-vlm-full-dir-review-2026-06-01/CN
```

Settings:

- Docling VLM pipeline evaluation route.
- Evaluation-only; not used as Route A full-document text/layout input.

Results:

- Formula nodes: 16.
- Number-only formulas: 0.
- Formula `(3)`: materially better candidate. No CJK/right-column prose
  contamination was observed in the formula candidate.
- Formula `(4)`: materially better candidate. The body `l_i = O(l_i) \times
  W_l` is recovered rather than only the equation number.
- Formula `(5)`: materially better candidate. The body is captured as a
  correlation-style fraction rather than the Granite repeated fraction-line
  hallucination.
- Formula `(16)`: the corresponding page-4 formula candidate has no repeated
  `and` chain, although numbering and exact expression quality still need
  manual review.

Assessment: real formula-quality improvement as a candidate source, but not a
safe full-document replacement. Previous manual review found that Route B drops
right-column text on CN.pdf pages 3 and 4, concatenates left-column text,
renders pictures as black blocks, and does not reliably handle inline formula
HTML. Route B should remain evaluation-only unless used in a bounded
formula-only role.

## Number-Only Formula Counts

| Variant | Formula nodes | Number-only formulas | Real improvement? |
| --- | ---: | ---: | --- |
| Route A Granite MLX baseline | 24 | 3 | No |
| Formula enrichment off | 24 | 0 | No; formula text is empty |
| Docling Server CodeFormulaV2 | n/a | n/a | No; conversion failed |
| Local Docling CodeFormulaV2 | n/a | n/a | No; model cache resolution failed |
| Route B VLM reference | 16 | 0 | Yes for formulas, but not safe as full-document route |

## Formula-by-Formula Summary

| Formula | Route A Granite MLX | Best observed candidate | Status |
| --- | --- | --- | --- |
| `(3)` | Includes `\text {所}` CJK/prose contamination despite plausible bbox. | Route B formula candidate avoids CJK/prose contamination. | Current Docling formula route fails. |
| `(4)` | Only `( 4 )`; body missing. | Route B candidate recovers `l_i = O(l_i) \times W_l`. | Current Docling formula route fails. |
| `(5)` | Bbox covers only a thin line; text is repeated fraction noise. | Route B candidate captures a full fraction-style body. | Current Docling formula route fails. |
| `(16)` | Long repeated `\ a n d` chain after formula. | Route B page-4 candidate has no repeated `and` chain. | Current Docling formula route fails. |

## Why Adapter-Level Repair Is Not Enough

The current adapter can improve traceability and warnings, but the failures are
not limited to evidence rendering:

- Formula `(3)` has a plausible left-column bbox and visually useful evidence
  crop, yet the enriched text is contaminated.
- Formula `(4)` is detected as only an equation number, so there is no formula
  body for the adapter to repair without adding a separate formula-region
  recovery pass.
- Formula `(5)` has a Docling formula bbox that is too thin for the visible
  body. Larger context crops help manual review but do not change the formula
  text produced by enrichment.
- Formula `(16)` shows runaway prose/token repetition from enrichment.

Post-processing could trim obvious CJK/prose tails or repeated `and` chains,
but that would only mask symptoms. It would not recover missing bodies for
formula `(4)` or formula `(5)`, and it would risk silently producing partial
formulas.

## Recommendation

Switch Route A formula extraction to a replacement strategy while preserving
Route A for the rest of the document contract:

1. Keep Docling Server standard pipeline for text, layout, OCR fallback, tables,
   pictures, page images, review index, and contract-compatible outputs.
2. Treat current Docling formula nodes as localization hints only when their
   bboxes are plausible.
3. Add a formula-only second pass over page or column crops, not a full-document
   VLM merge.
4. Use the VLM pipeline or another local formula OCR model only to propose
   formula candidates for detected/recovered formula regions.
5. Preserve evidence-first behavior: every replaced formula candidate should
   link to source crop, expanded context crop, and full page image, with warnings
   when candidate confidence is low.

Until that replacement exists, the safest Route A policy is to keep the
diagnostic review artifacts and explicitly mark suspicious formula text as
untrusted instead of presenting Docling formula enrichment as accurate.
