# Dependency and environment provenance

The active development line distinguishes dependencies built from source from
packages supplied by Ubuntu Server 24.04 LTS. Source dependencies are pinned to
immutable Git commits in `config/dependencies.env`. The expected upstream
versions of libyang, Protocol Buffers, and gRPC are recorded in the same file,
while the complete Debian revisions installed by APT are captured at runtime.

The default source revisions were recovered from the preserved SBRC 2026
development environment. They constitute the initial reproducible baseline for
post-SBRC development. Changing any revision is permitted only as a deliberate,
reviewed change accompanied by a new provenance record and regression tests.

A provenance report can be generated at any stage with:

```bash
./scripts/collect_provenance.sh
```

By default, the command writes `results/provenance/environment-provenance.txt` and a
corresponding SHA-256 file. An alternative output path may be supplied as the
first argument. The report records the operating system, kernel, allocated
resources, time configuration, repository revisions, dirty working trees,
relevant Debian packages, tool versions, Python packages, and network state.

The report is evidence of the environment that executed an experiment. It is
not a substitute for the dependency lock, and the dependency lock is not a
substitute for the report. Both are required for a defensible release.
