"""Datasets BioSense can reason over: where they live, what is known about them,
and who may see them.

Three roots, and the boundary between them is the privacy model:

    private_data/   datasets a person ingested. Never served, never committed,
                    never a citation. Default root; override with
                    BIOSENSE_PRIVATE_DATA.
    data_cache/     payloads fetched from public sources. Reproducible from the
                    accession, so git keeps the manifest and not the bytes.
    examples/datasets/
                    committed synthetic fixtures. Labelled as synthetic in their
                    own manifests so nothing mistakes them for measurements.

`roots.is_private_path` is the guard the HTTP servers use. It asks where a file
is, not what it is called: the older `*truth*` rule is a filename convention and
a person's dataset is not going to be named to suit it.
"""
