# MinerU Official Output Reproduction

## Summary

The official MinerU output path was reproduced successfully on macOS with the community MLX model:

```text
/Users/zeyuan/.cache/mineru/models/carlesonielfa--MinerU2.5-Pro-2604-1.2B-mlx-bf16
```

The successful path is the official `mineru` CLI with `-b vlm-auto-engine` and `--model-path`. On this Mac, `vlm-auto-engine` selects `mlx-engine`. The outputs are preserved exactly under MinerU's official structure:

```text
<output>/<file_stem>/vlm/
```

This confirms the next step should be a minimal official-artifact wrapper, not more custom crop extraction. The prior custom wrapper crop bug should be treated as wrapper-side until proven otherwise.

## Exact Official Command/API Attempted

Successful CLI pattern:

```bash
/usr/bin/time -l env MINERU_LOG_LEVEL=INFO /tmp/mineru-service-venv/bin/mineru \
  -p /path/to/input.pdf \
  -o services/mineru-service/reports/samples/mineru-official-output-2026-05-25/<sample> \
  -b vlm-auto-engine \
  -s <zero_based_start_page> \
  -e <zero_based_end_page> \
  --model-path /Users/zeyuan/.cache/mineru/models/carlesonielfa--MinerU2.5-Pro-2604-1.2B-mlx-bf16
```

Observed logs showed:

```text
Using mlx-engine as the inference engine for VLM.
get mlx-engine predictor cost: ~1.3-1.8s
```

Official `mineru-api` preload behavior was also checked:

- `mineru-api --enable-vlm-preload true --model-path <path>` fails in MinerU 3.1.15 with `ModelSingleton.get_model() got multiple values for argument 'model_path'`.
- `mineru-api --enable-vlm-preload true` works when using `MINERU_MODEL_SOURCE=local` and `MINERU_TOOLS_CONFIG_JSON` pointing to a config with `models-dir.vlm` set to the local MLX model path.

Working preload config:

```json
{
  "models-dir": {
    "vlm": "/Users/zeyuan/.cache/mineru/models/carlesonielfa--MinerU2.5-Pro-2604-1.2B-mlx-bf16"
  }
}
```

Working preload env:

```bash
MINERU_MODEL_SOURCE=local \
MINERU_TOOLS_CONFIG_JSON=/path/to/mineru.json \
/tmp/mineru-service-venv/bin/mineru-api --enable-vlm-preload true
```

## Config and Runtime

- Python runtime: `/opt/homebrew/bin/python3.13`, temporary venv at `/tmp/mineru-service-venv`.
- Official package installed into the temporary venv: `mineru[core]`.
- Installed MinerU package observed: `mineru 3.1.15`.
- Official package constraint downgraded `mineru-vl-utils` to `0.2.8`.
- `mlx-vlm` remained installed, so `vlm-auto-engine` selected `mlx-engine`.
- No global Python install, Docker, EXO, n8n, Docling, pipeline, or hybrid path was used for these runs.

Important dependency note: installing `mineru[core]` into the same temporary venv created version warnings because official MinerU 3.1.15 depends on `transformers<5`, while the earlier standalone `mlx-vlm` probe had installed `transformers>=5`. Despite this resolver warning, the official CLI VLM+MLX runs completed and produced artifacts.

## Official Output Tree

Official MinerU produced the expected files for all validated outputs:

```text
<sample>/<file_stem>/vlm/<file_stem>.md
<sample>/<file_stem>/vlm/<file_stem>_content_list.json
<sample>/<file_stem>/vlm/<file_stem>_content_list_v2.json
<sample>/<file_stem>/vlm/<file_stem>_middle.json
<sample>/<file_stem>/vlm/<file_stem>_model.json
<sample>/<file_stem>/vlm/<file_stem>_layout.pdf
<sample>/<file_stem>/vlm/<file_stem>_origin.pdf
<sample>/<file_stem>/vlm/images/
```

Output root preserved for manual review:

```text
services/mineru-service/reports/samples/mineru-official-output-2026-05-25/
```

The generated outputs are ignored by git and were not committed.

## Validation Table

| Sample | Pages | Command status | Markdown | content_list JSON | middle/model JSON | Images | Broken Markdown refs | Runtime | Memory note |
|---|---:|---|---|---|---|---:|---:|---:|---|
| `CN.pdf` | page 3 (`-s 2 -e 2`) | success | yes | yes, v1+v2 | yes | 12, all non-empty | 0 | 187.68 s | `/usr/bin/time`: max RSS 1.68 GB; peak footprint 0.29 GB, subprocess caveat |
| `table-heavy-ai-table-transformer.pdf` | page 1 (`-s 0 -e 0`) | success | yes | yes, v1+v2 | yes | 1, non-empty | 0 | 39.59 s | max RSS 3.16 GB; peak footprint 0.29 GB, subprocess caveat |
| `two-col-arxiv-ai-gat.pdf` | pages 1-2 (`-s 0 -e 1`) | success | yes | yes, v1+v2 | yes | 0 | 0 | 62.77 s | max RSS 3.29 GB; peak footprint 0.29 GB, subprocess caveat |

The memory values are the raw `/usr/bin/time -l` values from the top-level CLI command. Because the official CLI starts a temporary local `mineru-api` subprocess, these values should be treated as approximate process-tree observations rather than precise model memory accounting.

## Sample Quality Notes

### CN.pdf page 3

Official Markdown is readable and includes the formula 7 region:

```text
\boldsymbol {e} _ {q _ {i} \rightarrow c _ {p}} = \sum_ {h = 1} ^ {N} \boldsymbol {e} _ {h \rightarrow p}, \boldsymbol {Q} _ {i, h} = 1 \tag {7}
```

Counts from `CN_content_list.json`:

```text
text: 29
header: 2
equation: 10
image: 2
page_number: 1
```

The official images directory contains 12 non-empty JPG files. Markdown image references are valid. Manual inspection should focus on whether the 12 official images correspond to useful figure/formula evidence, but they are not blank at the file level.

### table-heavy-ai-table-transformer.pdf page 1

Official Markdown includes an HTML table with row/column spans:

```html
<table><tr><td rowspan="2" colspan="2"></td><td colspan="4">ΔSDM</td></tr>...</table>
```

Counts from `table-heavy-ai-table-transformer_content_list.json`:

```text
text: 15
table: 1
aside_text: 1
page_number: 1
```

The official images directory contains 1 non-empty JPG file. Markdown references are valid.

### two-col-arxiv-ai-gat.pdf pages 1-2

Official Markdown is readable and page ordered for the first two pages. No images were emitted for this bounded run, which is plausible for these pages.

Counts from `two-col-arxiv-ai-gat_content_list.json`:

```text
text: 26
header: 2
aside_text: 1
page_footnote: 1
page_number: 2
```

## Does Official CLI Support the Community MLX Model Directly?

Yes, for the CLI parse path:

```bash
mineru -b vlm-auto-engine --model-path /Users/zeyuan/.cache/mineru/models/carlesonielfa--MinerU2.5-Pro-2604-1.2B-mlx-bf16
```

This produced official `vlm/` outputs for all three samples.

## Does mineru-api Support Local VLM Preload?

Yes, but not with `--model-path` in MinerU 3.1.15.

- Failing preload path: `mineru-api --enable-vlm-preload true --model-path <path>`.
- Working preload path: `MINERU_MODEL_SOURCE=local` and `MINERU_TOOLS_CONFIG_JSON=<json>` with `models-dir.vlm=<path>`.

The failure appears to be an official argument plumbing issue, not a model incompatibility.

## Are Crops/Images Non-empty and Human-reviewable?

At the file level: yes/partial.

- CN: 12/12 images are non-empty and Markdown image refs resolve.
- Table-heavy: 1/1 image is non-empty.
- GAT pages 1-2: no image artifacts emitted.

This corrects the prior custom-wrapper finding: blank crops are not reproduced in the official output at the basic file/brightness level. Manual visual review is still needed to judge whether each official crop is semantically useful.

## Comparison to Docling Fallback

High-level only:

- CN formula 7: official MinerU detects and serializes it; this is better than the Docling fallback weakness on the same known case.
- Formula readability: promising for display formulas; inline/text-interleaved formulas still require manual review across more pages.
- Table output: official MinerU produced a directly rendered HTML table on the table-heavy sample.
- Image/crop usability: official images are non-empty and referenced correctly; use official images as the source of truth before custom crops.
- Artifact organization: official MinerU output is already close to what the service needs to preserve for n8n integration.

## Blockers

No blocker for official bounded VLM+MLX output reproduction.

Known caveats:

- Official full-document runtime may be high.
- `mineru-api` preload requires local config env, not `--model-path`.
- The temp venv now contains official `mineru[core]` plus earlier MLX packages and reports resolver warnings; a clean service runtime should be rebuilt with pinned compatible dependencies before production integration.
- Do not trust the custom wrapper's crop generation until it is replaced with official-artifact wrapping.

## Recommendation

Proceed to a minimal official-artifact wrapper plus later n8n integration design.

The next wrapper should not fabricate official-shape artifacts from blocks. It should:

1. invoke official `mineru -b vlm-auto-engine` or a preloaded official `mineru-api` using local config;
2. preserve `<file_stem>/vlm/` exactly;
3. copy or link official Markdown/JSON/images into Local AI Lab contract outputs with minimal transformation;
4. add only reference checks and metadata/status around official artifacts;
5. avoid custom crop extraction unless official artifacts are missing and that limitation is documented.
