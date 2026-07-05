# Extraction Scripts

Extraction scripts are the only sanctioned path for materializing
`projects/<domain>/extractions/{paper_id}.jsonl`.

Recommended agent flow:

1. The extractor writes or updates
   `projects/<domain>/extractions/.staging/{paper_id}/scripts/build.py`.
2. The build script imports the SDK:

   ```python
   from scripts.extraction.extraction_sdk import ExtractionBuilder, paper
   ```

3. The extractor runs:

   ```bash
   uv run scripts/extraction/run_extraction_build.py \
     --project projects/<domain> \
     --paper-id <paper_id> \
     --script projects/<domain>/extractions/.staging/<paper_id>/scripts/build.py \
     --finalize
   ```

4. `run_extraction_build.py` executes the build script, then finalizes through
   `extraction_builder.py`, which shares validation/materialization core with
   `write_extraction.py`.

Use `write_extraction.py` directly only for legacy or non-SDK staged rows.
Direct writes to extraction JSONL files are blocked by hooks.
