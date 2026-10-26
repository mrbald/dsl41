# Examples

- [Nightbank](nightbank/README.md): a large synthetic overnight bank and operator exercises.
- [Fulfilment](fulfilment/README.md): stock reservation, cancellation, and carrier reconciliation.
- [Energy](energy/README.md): metering revisions and settlement across engine periods.
- [Media](media/README.md): FFmpeg processing and immutable publication.
- [File processing](file_processing/README.md): experimental Python-authored archival,
  TODO handover and recoverable processing. [Design handover](file_processing/HANDOVER.md).
- [Soak](soak/README.md): a 200-job synthetic estate and the runner's capacity measurements.
- [Snapshots](snapshots/README.md): invented offline observations for the Nightbank and fulfilment graphs, for the viewer.
- [DST drill](dst-drill/README.md): the real runner on a fast fake clock across DST changes and midnight, compared with `dsl41 rehearse`.

The three local service examples share the setup in [workflows](workflows/README.md).
They use synthetic data and retain the evidence needed for later regression tests.
