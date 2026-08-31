# Plan: SPOT XML validation, merge, and CSV/Excel generation

## Current progress
- XML upload validation is in place and rejects malformed XML and schema-invalid files.
- Multi-file XML merge flow remains the core path for customers who already have SPOT-compatible XML exports.
- CSV/Excel-to-XML generation is implemented and validated against the official Puglia XSD.
- UI has a mode switch to support both merge and generation journeys without breaking current behavior.

## Next step
- Add a CSV template and field guidance so users without XML know the required columns before upload.
- Keep the merge flow unchanged and preserve the strict validation guardrails.
- Make the generator UX clearer with visible required-field hints and a downloadable sample file.

## Validation
- Use the existing test suite to verify that both XML merge and XML generation continue to pass after each UI improvement.
