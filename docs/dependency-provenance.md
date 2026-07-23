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

## Python dependency contract

The direct Python dependency contract is documented in
`requirements/python-runtime.in`. The complete environment recovered from the
preserved SBRC 2026 VM is fixed in `requirements/python-runtime.lock` and is the
only file consumed by `setup_all.sh python_env`.

The installer does not perform an unbounded upgrade of pip, setuptools, or
wheel. It installs the exact locked distributions, runs `pip check`, verifies
every locked version through Python package metadata, and then executes runtime
import checks. The provenance collector records both the lock-file hash and the
resolved environment reported by `pip freeze --all`.

The lock covers the framework runtime and the P4Runtime/NETCONF clients. Plotting
utilities are not included in this runtime lock because NumPy and Matplotlib
were not present in the preserved VM environment. Their dependency contract
must be established separately when the definitive analysis pipeline is
consolidated.
