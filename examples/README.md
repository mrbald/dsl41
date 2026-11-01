# Examples

For the job author choosing a synthetic estate to run, inspect and adapt.
Follow the [job author track](../README.md#documents) for estate acceptance
and [authoring](../docs/authoring.md) for the job contract. Engine installation
and recovery are covered by the [operator guide](../docs/deploy.md).

- [Nightbank](nightbank/README.md): a large synthetic overnight bank and operator exercises.
- [Fulfilment](fulfilment/README.md): stock reservation, cancellation, and carrier reconciliation.
- [Media](media/README.md): FFmpeg processing and immutable publication.
- [File processing](file_processing/README.md): experimental Python-authored archival,
  TODO handover and recoverable processing. [Design handover](file_processing/HANDOVER.md).
- The soak example is not in this tree. DL-317 in the [decision log](../docs/decision-log.md)
  records its 200-job measurements; DL-424 sets the engine's ceiling, which these
  examples do not qualify.
- [Snapshots](snapshots/README.md): invented offline observations for the Nightbank and fulfilment graphs, for the viewer.
- [DST drill](dst-drill/README.md): the real engine on a fast fake clock across DST changes and midnight, and on a clock that steps back, compared with `dsl41 rehearse`.

The two local service examples share the setup in [workflows](workflows/README.md).
They use synthetic data and retain the evidence needed for later regression tests.
