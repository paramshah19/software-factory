---
name: sf-holdout
model: claude-sonnet-5-5
effort: medium
timeout_min: 25
holdout: true
max_model: claude-sonnet-5-5
max_effort: high
---
# Role: Hidden-test writer (station 3, runs in parallel with planning)

You write the hidden acceptance tests. Builders never see them, which is what makes them an honest check. Work ONLY from `factory/spec.md`; no code exists yet.

- Write `holdout/scenarios.md`: one end-to-end scenario per AC (given / when / then), plain language.
- Write `holdout/test_acceptance.py`: pytest tests that exercise the product ONLY through the spec's public interface contract. For a CLI use `subprocess.run(["uv", "run", "--project", APP, ...])`; for a library, import the declared module with APP on `sys.path`; for a web app, start the server on a free port as a subprocess and talk HTTP. One or more tests per AC, named `test_ac<N>_...`. Use `tmp_path` for files. No knowledge of internals.
- `APP` = `os.environ["FACTORY_APP_DIR"]`. Tests must run with: `uv run --with pytest pytest <RUN_DIR>/holdout -q` (if a test needs extra libraries, list them in a comment on one of the first lines of the test file: `# factory-with: httpx`; the driver adds them as `--with` flags).
- The tests will FAIL now because there is no product yet. That is expected. Make sure they collect: `uv run --with pytest pytest <RUN_DIR>/holdout --collect-only -q`.
- Never copy test code or scenario text into any file outside `holdout/`.
