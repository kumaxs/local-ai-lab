# Release architecture

Version `1.0.1` ships as two runtime profiles with one public contract.

The distributable release is anchored by Git tag `v1.0.1`. Release automation
produces SHA-256-verified `.tar.gz` and `.zip` bundles, verifies the macOS bundle
from a clean runner, publishes all three Docker images for `linux/amd64` and
`linux/arm64`, and attaches the bundles and checksums to a GitHub Release.
Prebuilt images include OCI revision metadata, provenance, and SBOM
attestations. See [DISTRIBUTION.md](DISTRIBUTION.md) for installation and
integrity verification.

| Capability | macOS release | Docker release |
| --- | --- | --- |
| Public API | `/v1/jobs` and `/v1/jobs/{id}` | identical |
| Standard PDF pipeline | Docling Serve, CPU by default | Docling Serve, CPU by default |
| Formula model | Granite Docling with MLX on Apple Silicon; Transformers diagnostic path on Intel | isolated, source-gated UniMERNet-Small primary plus PP-FormulaNet-L fallback |
| OCR fallback | OCRMac full-page OCR when `/Gxx` quality gates fail | portable automatic OCR backed by RapidOCR |
| Table extraction | accurate mode and cell matching | identical |
| Semantic HTML/Markdown | shared accepted semantic reflow | identical |
| Review evidence | source crops and structured sidecars | identical |
| Concurrency | one conversion by default | one conversion by default |

The public service separates Docling Serve from the Local AI Lab API. Docling
Serve owns document model execution. The API owns uploads, the bounded job
queue, the accepted quality policy, semantic reconstruction, durable state,
output manifests, and safe file downloads. Docker adds a third private formula
process. Keeping those responsibilities separate allows Docker to replace
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
rendering. Docker leaves Docling formula enrichment disabled after both Granite
Transformers and CodeFormulaV2 failed the formula-dense release gate with raw,
incomplete, or non-renderable output. Formula regions remain available from
layout detection and are recognized by a dedicated guarded formula container.

UniMERNet-Small is the primary Docker formula recognizer. Its output must be
structurally safe and cover the meaningful operators, relations, Greek symbols,
and set symbols extracted from the same PDF bounding box. The adapter first
tightens the layout crop to visible formula ink. A failed coverage or structure
gate, and a small set of model-observed ambiguous symbol structures, trigger an
independent PP-FormulaNet-L visual cross-check on that tightened crop. Only an
edge-clipped preview selects a column-bounded crop rendered at six times the
original PDF resolution. The primary model is released before the fallback
loads; single-item batches and sequential model residency keep peak memory
practical on Docker Desktop. A fallback that still fails structure or source
coverage is rejected.
The adapter clears Docling Serve's converter cache after each Docker conversion
response, before the formula sidecar loads a recognizer, so parser and formula
model peaks do not overlap. The macOS profile deliberately keeps its backend
cache because its MLX formula path shares the normal platform runtime.
If `/Gxx` detection proves that the original PDF text layer is corrupt, source
coverage is disabled for that job and both candidates remain subject to strict
structural and hallucination checks. This is evidence-driven and contains no
filename-, title-, or paper-specific formula replacements.

The formula container has its own PyTorch/Transformers/Paddle dependency graph,
model volume, health check, bounded batch size, and private network endpoint. It
has no published host port. The adapter accepts only `http://formula:8001` or
loopback, so paper-derived formula crops and source text cannot be redirected to
an arbitrary remote service. Formula recognition must patch every detected
formula, Markdown must contain every TeX block, and HTML must contain renderable
MathML for every semantic formula; otherwise the job is a degraded failure.
The macOS MLX dependency lock remains separate.
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
