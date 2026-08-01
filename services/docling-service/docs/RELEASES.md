# Release architecture

Version `1.0.0` ships as two runtime profiles with one public contract.

| Capability | macOS release | Docker release |
| --- | --- | --- |
| Public API | `/v1/jobs` and `/v1/jobs/{id}` | identical |
| Standard PDF pipeline | Docling Serve, CPU by default | Docling Serve, CPU by default |
| Formula model | Granite Docling with MLX on Apple Silicon; Transformers on Intel | Granite Docling with Transformers |
| OCR fallback | OCRMac full-page OCR when `/Gxx` quality gates fail | portable automatic OCR backed by RapidOCR |
| Table extraction | accurate mode and cell matching | identical |
| Semantic HTML/Markdown | shared accepted semantic reflow | identical |
| Review evidence | source crops and structured sidecars | identical |
| Concurrency | one conversion by default | one conversion by default |

The service consists of two processes. Docling Serve owns model execution. The
Local AI Lab API owns uploads, the bounded job queue, the accepted quality
policy, semantic reconstruction, durable state, output manifests, and safe file
downloads. Keeping those responsibilities separate allows Docker to replace
platform-specific model engines without forking the output logic.

The primary HTML and Markdown are real semantic outputs. Page or region images
are review evidence only; they never replace the paper body, formulas, tables,
algorithms, or code listings.

## Quality invariants

Both profiles retain these release requirements:

- algorithm and code indentation is preserved as semantic preformatted content;
- bold, italic, keyword, comment, string, and number emphasis is retained where
  reliable source evidence exists;
- formulas are represented by TeX-backed MathML in HTML and TeX in Markdown;
- tables preserve rows, columns, cell line breaks, check/cross symbols, and
  separate JSON/HTML/CSV evidence where available;
- citations link to bibliography entries and footnote callouts link to their
  corresponding notes;
- headers, footers, visual annotation, glyph noise, and quarantined OCR fragments
  do not enter the main reading flow;
- unresolved or degraded quality is reported in `status.json` instead of being
  silently called success.

## Docker-specific refactor

The Docker profile never requests `granite_mlx`, `ocrmac`, Metal, Apple Vision,
or MPS. Its backend image includes portable fonts (Noto CJK, Noto Mono, DejaVu,
and STIX), a Linux OCR engine, a persistent model cache, a single shared model
worker, bounded API concurrency, and a larger shared-memory allocation for PDF
rendering. Formula execution does not request quantization and disables model
compilation in the portable CPU profile to avoid unsupported host assumptions.
The backend allowlist adds only Docling's built-in `granite_docling` formula
preset alongside `default`; arbitrary presets and remote engines remain closed.
The Serve synchronous wait and per-document timeout both follow the release
conversion timeout, preventing long formula jobs from being duplicated by
premature gateway retries.
The image installs CPU-only PyTorch wheels and omits the unused
Kubernetes and Ray execution backends from Docling JobKit. Docling Serve imports
its RQ orchestrator module at process startup, so the image retains only RQ's
small import dependency set; no Redis/RQ service is started and conversions use
only the local engine. The build context excludes the 66 MB development report
corpus, tests, caches, and local environments; the API image also separates its
locked dependency layer from application source so code-only rebuilds stay fast.

Model and OCR engine substitutions can produce small recognition differences.
The semantic repair, quality gates, API, and output schema are shared, and every
job records the actual formula/OCR runtime in its metadata.
