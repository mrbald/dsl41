# Next work

- [ ] After the file-processing building block: improve the Python estate interface
  produced by `dsl41 decompile`. Make the output easy to import, configure, execute
  and maintain as the starting point for migration from JIL. Use synthetic estates
  to develop a migration guide. Preserve canonical equivalence, explicit conditions
  and the closed fold registry. Decide a supported Python execution entry point
  before adding domain-specific decompiler recognition. The file-processing
  prototype in `examples/file_processing/` is the first authoring use case.
