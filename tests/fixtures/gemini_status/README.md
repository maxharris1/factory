Replies of `agy models </dev/null`, the doctor's Gemini status probe, run with
every Gemini auth override and `GEMINI_CLI_HOME` unset. Each reply's stdout is
in `<name>.out`, its stderr in `<name>.err` and its exit code in `<name>.code`.
A missing stream is empty. The replies come from the #1639 live check of
Antigravity `agy` 1.5.15:

- `signed-in`: the device's `gemini-bot` login is signed in, so `agy models`
  exits 0 and lists the models on stdout, one row each, the model ID padded to
  a column then the display name. The rows are a subset of Google's documented
  output (https://antigravity.google/docs/cli/headless/#select-a-model-effort-or-agent)
  and were not captured. A later live capture can replace them.
- `one-model`: exit 0 with one row whose display name follows the model ID
  after a single space, which the doctor reads as signed in.
- `signed-out`: no login, exit 1 with the CLI's exact sign-in error.
- `no-models`: exit 0 with no output, which the doctor does not read as signed in.
- `no-models-text`: exit 0 with the sentence `No models available` on stdout,
  which the doctor does not read as signed in because the sentence is not a
  model row.
- `sign-in-text`: exit 0 with the sign-in sentence on stdout and no `Error:`
  prefix, which the doctor does not read as signed in or signed out.
- `cache-warning`: exit 0 with a warning only, which the doctor does not read as
  signed in.
- `warning-word`: exit 0 with one word on stderr, which the doctor does not read
  as signed in.
- `models-with-warning`: exit 0 with the model list on stdout and a warning on
  stderr, which the doctor reads as signed in.

`agy models` sends no prompt to a model.
