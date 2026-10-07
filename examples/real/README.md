# Real examples

Runs made with a real AI runtime, exported so a reader can see what BioSense
did on a real question — the objective, the hypotheses, the protocol with its
provenance and gaps, the notes the agents wrote, and the limitations — without
anything that must not be committed.

## Adding one

1. **Run it.** In the web app (AI Discovery), choose your project, write the
   objective, keep *Public databases* on, choose a real runtime and start it.
   Watch it live on AI Discovery or on the Loop Tracker.
2. **Export it.** When it has finished:

   ```bash
   uv run --frozen python -m biosense.production.export_example \
     --run runs/ai-YYYYMMDD-xxxxxx --name short-descriptive-name --out examples/real
   ```

   On a hosted deployment the run directory is on the volume (`/app/data/runs`);
   download it first, or run the export there.
3. **Add pictures.** Screenshot the protocol timeline, the insights panel and
   the Loop Tracker, save them in the new folder (`timeline.png`, …) and
   reference them in its `README.md`.
4. **Check before committing.** Read the folder: the export leaves out full
   texts (`sources/`), raw logs (`events.jsonl`), caches, drafts and anything
   computed from private data, and lists what it withheld. Quotes from papers
   in the notes should be short.
5. **Link it** from the main README's *Real examples* section and open a pull
   request.

The export refuses a synthetic demo run: that one already lives in the
repository as the worked demonstration, and calling it real would mislead.

## Examples

*None yet — the first real run will be listed here.*
