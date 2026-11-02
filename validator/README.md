# nonceworks-validate

Static checks for OpenZeppelin Relayer and Monitor configuration. It reads a config directory, a ZIP archive (for example a Defender export) or a single JSON file, and reports errors, warnings and notices with a rule id each. Nothing is executed and nothing is sent anywhere. Standard library only, Python 3.9 or later.

```bash
pip install "git+https://github.com/zachsplat/relayer-failure-lab#subdirectory=validator"
nonceworks-validate config/                 # text report; exit 1 on errors
nonceworks-validate --strict config/        # exit 1 on warnings too
nonceworks-validate --format json export.zip
nonceworks-validate --list-rules
```

Exit codes: 0 no errors (and no warnings with `--strict`), 1 findings that fail the check, 2 the input could not be read.

The warnings for #808, #843, #912 and the notice for #817 come from the scenarios in the [Relayer Failure Lab](../README.md), which runs them against every Relayer release. The checks started as the ones behind https://nonceworks.com/validate.html.

MIT. Not affiliated with OpenZeppelin.
