# Chinese research notes

Each `work<series>.json` contains an unofficial Chinese-language research note based on a BIS working paper. These notes are not official BIS translations, and the BIS does not guarantee their accuracy or completeness. The original content is available on the BIS website; the application links to each paper's original page and PDF.

The notes include research questions, findings, methods, evidence, contribution, limitations, implications and optional research ideas. Findings and evidence should be attributed to the authors; limitations and implications can include the note writer's judgment. Review statuses record checks performed during preparation, not a guarantee. Always check claims, table references, coefficients and significance against the original paper before citing.

The app loads these files into SQLite on startup. To add a note, follow the existing JSON schema, include a citation and source, and verify evidence against the PDF. `tools/pdftext.py` can help locate text and numbers; automated matches alone do not establish correct attribution. Use `tools/page_offset.py` to calculate page offsets, and manually check cases where appendix numbering restarts. Preserve significance stars such as `0.35**`: they are data, not Markdown emphasis.

BIS material remains subject to [BIS terms](https://www.bis.org/about/terms-conditions). Project code's MIT license does not grant rights to BIS material.
